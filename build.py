#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGH-Builder v2.0 — 深度改写自 qq5460168/666 规则合并项目，只适配 AdGuard Home。

原项目每次更新会生成 11 种格式（Clash/Quantumult X/Shadowrocket/SingBox…），
本程序砍掉所有非目标格式，把全部精力放在一件事上：
    —— 产出 AdGuard Home 能 100% 生效的 DNS 层规则 ——

与 AdGuard Home 的适配原则（DNS 层看不到 HTTP 内容）：
  ✗ 剔除 元素隐藏规则  ## / #@#           （浏览器 DOM 操作，DNS 层无效）
  ✗ 剔除 JS 注入规则    #%# / #@%#         （同上）
  ✗ 剔除 HTTP 层修饰符  $replace $script $image $media $font $object
                        $xhr $subdocument $frame $popup $document
                        $elemhide $generichide $redirect ...          （DNS 层无此概念）
  ✗ 剔除 带 URL 路径的规则（||domain/path、domain/path、/path）       （DNS 查询只有域名）
  ✗ 剔除 纯 IP / IP:端口 / IPv6 条目
  ✓ 保留 ||domain^ 与 @@||domain^ 及其支持的修饰符
  ✓ 保留 /regex/ 正则规则（AGH 支持，可关闭）
  ✓ hosts / 纯域名行 自动归一化为 ||domain^

用法：
    python3 build.py                 # 使用默认 sources.txt / allow-sources.txt
    python3 build.py --no-regex      # 不保留正则规则
    python3 build.py --no-hosts      # 不生成 hosts 格式
    python3 build.py --no-strict     # 白名单冲突仅精确匹配（默认严格：子域名也剔除）

输出（dist/ 目录）：
    adguard-home-rules.txt      AGH 拦截规则（可直接作为 DNS 拦截列表订阅）
    adguard-home-allowlist.txt  AGH 白名单（可加入自定义过滤规则）
    hosts.txt                    hosts 格式（AGH 亦支持）
    stats.json                   JSON 统计
"""

import argparse
import concurrent.futures as cf
import datetime
import fnmatch
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.error import HTTPError

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"

CONFIG = {
    "sources_file": BASE_DIR / "sources.txt",
    "allow_sources_file": BASE_DIR / "allow-sources.txt",
    "out_rules": DIST_DIR / "adguard-home-rules.txt",
    "out_allow": DIST_DIR / "adguard-home-allowlist.txt",
    "out_hosts": DIST_DIR / "hosts.txt",
    "out_stats": DIST_DIR / "stats.json",
    "max_workers": 8,          # 并发下载数
    "timeout": 30,             # 单次下载超时（秒）
    "retries": 3,              # 单源连续重试次数
    "retry_wait": 10,          # 第一轮失败后，第二轮补拉前的等待秒数（网络抖动多为时间窗口性）
    "max_source_bytes": 100 * 1024 * 1024,  # 单源下载上限（100MB），防异常响应撑爆内存
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (compatible; AGH-Builder/2.0)",
    ],
    "title": "sofmed DNS",
    "homepage": "sofmed DNS提供",
    "keep_regex": True,        # 保留 /regex/ 正则规则
    "keep_hosts": True,        # 同时输出 hosts 格式
    "strict_conflict": True,   # 白名单子域名也剔除对应黑名单
}

# AdGuard Home DNS 过滤官方支持的修饰符（adguard-dns.io/kb/general/dns-filtering-syntax/）
# 文档明确：规则含未列出修饰符时整条被忽略。因此只保留官方确认集。
SUPPORTED_MODIFIERS = {
    "client", "denyallow", "dnstype", "dnsrewrite", "important", "badfilter", "ctag",
}

# 明确不支持的修饰符（HTTP/内容层/浏览器概念/仅AdGuard DNS）——命中即剔除整条规则
UNSUPPORTED_MODIFIERS = {
    "replace", "script", "stylesheet", "image", "media", "font", "object",
    "xhr", "xmlhttprequest", "subdocument", "frame", "popup", "document",
    "elemhide", "generichide", "jsinject", "popunder", "webrtc",
    "inline-script", "inline-font", "other", "ping", "websocket", "csp",
    "redirect", "redirect-rule", "removeparam", "removeheader", "queryprune",
    "match-case", "empty", "mp4", "permissions", "stealth", "urlskip",
    "importantnt", "importa",  # 上游常见笔误，一并剔除
    # 浏览器/HTTP 层概念（DNS 层无意义，AGH 会忽略含它们的规则）
    "third-party", "domain", "network", "app", "all",
    # 仅 AdGuard DNS 支持（AGH 不支持）
    "respgeo",
}

# 纯域名（含通配符 * ）合法字符
DOMAIN_CHARS = re.compile(r"^[a-zA-Z0-9_\-.*]+$")
# hosts 行 IP 前缀（只匹配 0.0.0.0/127.0.0.1/... 及其后空白），
# 后面跟的所有主机名 token 逐个处理，以支持一行多主机名：0.0.0.0 a.com b.com c.com
HOSTS_IP_PREFIX = re.compile(
    r"^\s*(?:0\.0\.0\.0|127\.0\.0\.1|255\.255\.255\.255|::1|::)\s+",
    re.IGNORECASE
)
# hosts 行：可选 IP 前缀 + 单域名（兼容旧逻辑，实际多主机名由 HOSTS_IP_PREFIX 拆分）
HOSTS_LINE = re.compile(
    r"^\s*(?:0\.0\.0\.0|127\.0\.0\.1|255\.255\.255\.255|::|::1)\s+"
    r"([a-zA-Z0-9_\-.*]+\.[a-zA-Z0-9_\-.*]+)(?:\s+#.*)?\s*$", re.IGNORECASE
)
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# 正则规则：/xxx/ 或 /xxx/$mod
REGEX_RULE = re.compile(r"^/.*/(?:\$[^/]*)?$")
# 布尔型修饰符（无值）
BOOL_MODS = {"important", "badfilter"}
# hosts 不应包含的条件修饰符（hosts 无条件生效）
COND_MODS = ("client", "dnstype", "ctag", "denyallow", "dnsrewrite", "badfilter")
# 本地保留名（hosts 文件中的系统占位，不应出现在拦截列表）
RESERVED_NAMES = {"localhost", "local", "loopback", "broadcasthost", "ip6-localhost",
                  "ip6-loopback", "ip6-localnet", "ip6-mcastprefix", "ip6-allnodes",
                  "ip6-allrouters", "ip6-allhosts", "0.0.0.0", "::", "::1"}

# 邮件认证 / 服务发现标签（均为 TXT/MX 类记录，不是可访问主机）。
# AGH 的 ||domain^ 默认匹配该名所有查询类型，若拦截这些标签会破坏 DKIM/DMARC/SPF
# 邮件认证，导致正常邮件被判为垃圾邮件，且无任何广告拦截价值，一律剔除。
MAIL_AUTH_LABELS = {
    "_domainkey", "_dmarc", "_spf", "_amazonses", "_mta-sts", "_dkim",
    "_autodiscover", "_autoconfig", "_imap", "_smtp", "_pop", "_pop3",
}


def is_mail_auth_record(domain: str) -> bool:
    """域名任一段是邮件认证/服务发现标签（如 10dkim1._domainkey.bank.com）"""
    return any(p.lower() in MAIL_AUTH_LABELS for p in domain.split("."))


# ============================================================
# 日志与工具
# ============================================================
def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def decode_bytes(data: bytes) -> str:
    """多编码兜底解码（上游源编码混乱，utf-8 → gbk → latin-1）"""
    for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def decode_text(data: bytes) -> str:
    """解码并清理 BOM / 空字符，返回文本字符串"""
    text = decode_bytes(data)
    if text and text[0] == "\ufeff":      # 去 UTF-8 BOM
        text = text[1:]
    return text.replace("\x00", "")


def is_ip(domain: str) -> bool:
    return bool(IPV4_RE.match(domain)) or ":" in domain  # IPv4 或含冒号（IPv6/端口）


def is_ip_wildcard(domain: str) -> bool:
    """IP 段通配（如 111.59.67.* / 172.247.107.16*）：所有段仅由数字与 * 组成。
    完整 IP 规则已被 is_ip 剔除；IP 段带通配同样不是合法域名，AGH 不支持 IP 通配语法，
    故一并剔除，避免伪域名漏网。域名通配（*.example.com / *adintl.cn）含字母段，不受影响。"""
    if "*" not in domain:
        return False
    parts = domain.split(".")
    return len(parts) >= 2 and all(p and re.fullmatch(r"[0-9*]+", p) for p in parts)


def is_valid_domain(domain: str) -> bool:
    """
    域名合法性检查（通配符 * 允许，但剔除保留名与畸形串）：
    - 剔除 localhost / loopback 等系统保留名（含其子域）
    - 剔除 _domainkey / _dmarc 等邮件认证标签（拦截会破坏邮件，见 MAIL_AUTH_LABELS）
    - 剔除 IP 段通配（111.59.67.*，非域名，AGH 不支持 IP 通配）
    - 每段不允许以 - 开头/结尾、不允许空段、单标签不超过 63 字符（RFC 1035）
    - 总长不超过 253
    """
    if not domain or len(domain) > 253:
        return False
    d = domain.rstrip(".")
    if d.lower() in RESERVED_NAMES or d.lower().endswith(".localhost"):
        return False
    if is_mail_auth_record(d):
        return False
    if is_ip_wildcard(d):
        return False
    for part in d.split("."):
        if not part:
            return False
        if len(part) > 63:            # RFC 1035：单个标签最长 63 字符
            return False
        if part.startswith("-") or part.endswith("-"):
            return False
    return True


def extract_domain(rule: str) -> str:
    """从 ||domain^ / @@||domain^ / 正则规则中提取纯域名（用于冲突检测）"""
    r = rule
    if r.startswith("@@"):
        r = r[2:]
    if r.startswith("||"):
        r = r[2:]
    if "$" in r:
        r = r.split("$", 1)[0]
    if "^" in r:
        r = r.split("^", 1)[0]
    if "/" in r:
        r = r.split("/", 1)[0]
    return r.lower()


def regex_domain_candidates(rule: str) -> set:
    """从正则拦截规则 /ads\\.example\\.com/$important 提取可能命中的域名候选。
    用于白名单冲突剔除：候选域名若被白名单覆盖，则该正则规则会误拦截，应剔除。
    无法可靠提取时返回空集，调用方按"保留"处理，宁缺毋滥。"""
    r = rule
    if r.startswith("@@"):
        r = r[2:]
    if r.startswith("/"):
        r = r[1:]
    if r.endswith("/"):
        r = r[:-1]
    if "$" in r:
        r = r.split("$", 1)[0]
    s = r.replace(r"\.", "\x00")          # 转义点 \. 是真实域名点，占位保护
    toks = re.split(r"[^a-z0-9\x00.-]", s)   # 其余元字符一律分隔
    cands = set()
    for tok in toks:
        tok = tok.replace("\x00", ".").strip(".-").lower()
        if tok.count(".") >= 1 and ".." not in tok \
                and re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", tok):
            cands.add(tok)
    return cands


# ============================================================
# 下载
# ============================================================
def fetch_source(source: str):
    """
    读取一个源：file: 本地文件 或 http(s) 网络地址，返回按行分割的文本。
    返回值约定：
      - list  读取成功（含空内容）或本地文件确定性失败（不参与补拉）
      - None  网络源重试耗尽（临时性失败，调用方可做第二轮补拉）
    """
    if source.startswith("file:"):
        path = Path(source[5:].strip())
        # 安全限制：本地文件必须位于程序目录内，拒绝 ../ 路径穿越
        full = BASE_DIR / path
        try:
            full = full.resolve()
            _base = BASE_DIR.resolve()
            try:
                full.relative_to(_base); _inside = True
            except ValueError:
                _inside = False
            if not _inside:
                log(f"  ✗ 本地文件超出程序目录，已拒绝: {source}")
                return []
            if not full.exists():
                log(f"  ✗ 本地文件不存在: {source}")
                return []
            return decode_text(full.read_bytes()).splitlines()
        except Exception as e:
            log(f"  ✗ 读取本地文件失败 {source}: {e}")
            return []

    for attempt in range(CONFIG["retries"]):
        try:
            req = urllib.request.Request(
                source, headers={
                    "User-Agent": random.choice(CONFIG["user_agents"]),
                    "Accept-Encoding": "gzip, deflate",
                }
            )
            with urllib.request.urlopen(req, timeout=CONFIG["timeout"]) as resp:
                # 流式分块读取并实时累计，超过上限即中止，
                # 避免上游异常响应一次性 read() 撑爆内存（原实现先全量读入再判大小）
                chunks, total = [], 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > CONFIG["max_source_bytes"]:
                        log(f"  ✗ 源过大（>{CONFIG['max_source_bytes']//1024//1024}MB），已拒绝: {source}")
                        return []
                    chunks.append(chunk)
                raw = b"".join(chunks)
                # 透明解压（请求头声明了 gzip/deflate，服务器可能据此压缩）
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return decode_text(raw).splitlines()
        except HTTPError as e:
            # 4xx（除 429 限流）是永久性错误，重试与第二轮补拉都无意义，直接放弃（不补拉）
            if 400 <= e.code < 500 and e.code != 429:
                log(f"  ✗ 下载失败（HTTP {e.code}，永久错误不重试）: {source}")
                return []
            if attempt >= CONFIG["retries"] - 1:
                log(f"  ✗ 下载失败（{CONFIG['retries']} 次）: {source}  (HTTP {e.code})")
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            if attempt >= CONFIG["retries"] - 1:
                log(f"  ✗ 下载失败（{CONFIG['retries']} 次）: {source}  ({str(e)[:80]})")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


# ============================================================
# AGH 兼容性清洗（核心）
# ============================================================
# 修饰符值的合法字符（不允许引号/^/空格等；~ 为排除语法，; 为 dnsrewrite 分隔，* 为 denyallow 通配）
MOD_VALUE_CHARS = re.compile(r"^[a-zA-Z0-9_.:|\-~;*]*$")
# client 值：非引号时允许 CIDR（192.168.0.0/16）、IP、域名、域名列表（| 分隔）
CLIENT_VALUE_CHARS = re.compile(r"^[a-zA-Z0-9_.:|\-/]+$")


def _sanitize_mods(mods: str) -> str:
    """清理修饰符段：去首尾空白，并剥掉尾部误带的 ^（及空白）。
    上游常有 `||x^$important^`、`||x^$client='1.2.3.4'^` 这种从其他格式复制来的尾部 ^，
    它不是合法修饰符语法，会让整条规则被 AGH 忽略。^ 是 URL 分隔符，绝不可能合法出现在
    修饰符列表末尾，因此安全剥离。"""
    return mods.strip().rstrip("^").strip()


def check_modifiers(mods: str) -> bool:
    """校验修饰符列表是否全部被 AGH 支持且值合法；任一异常返回 False"""
    mods = _sanitize_mods(mods)
    if not mods:
        return True
    for item in mods.split(","):
        raw = item.strip()
        if not raw:
            continue
        negated = raw.startswith("~")
        item = raw.lstrip("~")
        name, sep, value = item.partition("=")
        name = name.lower()
        # 布尔修饰符不支持 ~ 否定（~important / ~badfilter 无意义，AGH 不识别）
        if negated and name in BOOL_MODS:
            return False
        if name in UNSUPPORTED_MODIFIERS:
            return False
        # 未知修饰符：保守起见也剔除（宁可少杀，不可让 AGH 解析告警）
        if name not in SUPPORTED_MODIFIERS:
            return False
        # 无值修饰符只允许布尔型；client/dnstype/dnsrewrite/ctag/denyallow 必须有值
        if not sep:
            if name not in BOOL_MODS:
                return False
            continue
        # 布尔修饰符不允许带值（badfilter=xxx / important=xxx 非法，AGH 不识别）
        if name in BOOL_MODS:
            return False
        # 有 = 但值为空（如 dnstype= / client= / dnsrewrite=）：同样剔除
        if not value:
            return False
        # client 支持引号包裹的客户端名（官方语法：$client='My Client' / $client=~'排除'）
        # 或 CIDR/IP/域名列表（$client=192.168.0.0/16 / $client=pc.example.com|server.example.com）
        if name == "client" and value:
            v = value[1:] if value.startswith("~") else value  # 排除前缀 ~ 在引号外
            if v and v[0] in "'\"":
                if len(v) >= 2 and v[-1] == v[0]:
                    continue
                return False
            if not CLIENT_VALUE_CHARS.match(v):
                return False
            continue
        # dnsrewrite 值可含空格/分号（如 NOERROR;HTTPS;32 example.com alpn=h3），仅禁换行与 ^
        if name == "dnsrewrite":
            if "\n" in value or "^" in value:
                return False
            continue
        # 其余修饰符：值须在合法字符集内（引号、^、空格、/ 等视为上游私人定制/格式错误）
        if not MOD_VALUE_CHARS.match(value):
            return False
    return True


def normalize_mods(mods: str) -> str:
    """
    修饰符值统一小写（域名大小写不敏感，避免去重漏网）。
    例外（值大小写敏感，保持原样或强制大写）：
    - dnsrewrite：值复杂（REFUSED/NOERROR/IP/HTTPS 记录…），保持原样
    - dnstype：记录类型必须大写（A、AAAA、MX、TXT、CNAME…），强制 upper()
    - client：客户端名大小写敏感（用户定义的 "PC" 和 "pc" 是不同客户端），保持原样
    """
    mods = _sanitize_mods(mods)
    parts = []
    for item in mods.split(","):
        if not item:
            continue  # 跳过空项（尾部逗号、双逗号）
        name, sep, value = item.partition("=")
        if not sep:
            parts.append(item)
            continue
        # ~ 排除前缀不影响值大小写策略（~client=MyPhone 与 client=MyPhone 同样保持原样）
        nm = name.lstrip("~").lower()
        # 修饰符名统一小写（AGH 按小写识别；上游可能出现 $DNSType= 等大写变体）
        lname = name.lower()
        if nm == "dnsrewrite":
            parts.append(f"{lname}={value}")          # 值复杂，保持原样
        elif nm == "dnstype":
            parts.append(f"{lname}={value.upper()}")  # 记录类型必须大写
        elif nm == "client":
            parts.append(f"{lname}={value}")          # 客户端名大小写敏感，值保持原样
        else:
            parts.append(f"{lname}={value.lower()}")
    return ",".join(parts)


def clean_rule(line: str, keep_regex: bool):
    """
    清洗单行规则。
    返回 [(kind, rule), ...]，每个元素表示一条可用规则；
    返回 [] 表示该行对 AGH 无意义，应剔除。
    一条 hosts 行可能产出多条（0.0.0.0 a.com b.com c.com），故返回列表。
    """
    line = line.strip()
    if not line:
        return []
    # 全角双竖线 ‖(U+2016) 常是 || 的手误等价物（DNS 层只认 ASCII），规范化为 ||
    if line.startswith("‖"):
        line = "||" + line[1:]

    # 1) 注释（! 开头；# 开头但非元素规则）
    if line.startswith("!"):
        return []
    # 2) 元素隐藏 / JS 注入（必须先于 # 注释判断）
    if "##" in line or "#@#" in line or "#%#" in line or "#@%#" in line:
        return []
    if line.startswith("#"):
        return []

    # 3) 正则规则 /xxx/ 或 /xxx/$mod，及白名单正则 @@/xxx/（官方语法：@@/example.*/$important）
    inner = line[2:] if line.startswith("@@") else line
    if (line.startswith("/") or line.startswith("@@/")) and REGEX_RULE.match(inner):
        if not keep_regex:
            return []
        # 校验正则规则上的修饰符；尾部空修饰符（/xxx/$）去掉 $
        if "$" in inner:
            mods = _sanitize_mods(inner.split("$", 1)[1])
            if mods:
                if not check_modifiers(mods):
                    return []
                inner = inner.split("$", 1)[0] + "$" + normalize_mods(mods)
            else:
                inner = inner.split("$", 1)[0]
        return [("regex", ("@@" if line.startswith("@@") else "") + inner)]

    # 4) 白名单 @@ 规则
    is_allow = line.startswith("@@")
    body = line[2:] if is_allow else line

    # 5) hosts 行 / 纯域名行归一化（不含 || 前缀的普通行）
    if not body.startswith("||"):
        # 5a) |domain^ / |domain^$mod（| 为域名开头锚点，官方语法；DNS 层等价于 || 前缀）
        tmp = body
        if tmp.startswith("|") and not tmp.startswith("||"):
            d = tmp[1:]
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            d = d[:-1] if d.endswith("^") else d
            d = d.rstrip(".")
            if (
                "/" not in d and ":" not in d
                and DOMAIN_CHARS.match(d) and "." in d
                and not is_ip(d) and is_valid_domain(d)
                and (not dmods or check_modifiers(dmods))
            ):
                rule = ("@@" if is_allow else "") + f"||{d.lower()}^"
                if dmods:
                    rule += f"${normalize_mods(dmods)}"
                return [("domain", rule)]
        # 5b) domain^ / domain^$mod（无 || / | 前缀；AdGuard 无锚定 substring 语法）
        if not body.startswith("|"):
            d = body
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            if d.endswith("^"):
                d = d[:-1].rstrip(".")
                if (
                    "/" not in d and ":" not in d
                    and DOMAIN_CHARS.match(d) and "." in d
                    and not is_ip(d) and is_valid_domain(d)
                    and (not dmods or check_modifiers(dmods))
                ):
                    rule = ("@@" if is_allow else "") + f"||{d.lower()}^"
                    if dmods:
                        rule += f"${normalize_mods(dmods)}"
                    return [("domain", rule)]
        # 5c) hosts 行：IP 前缀后可能跟多个主机名（0.0.0.0 a.com b.com c.com），逐个产出
        m = HOSTS_IP_PREFIX.match(line)
        if m:
            rest = line[m.end():]
            c = re.search(r"\s+#", rest)          # 去行尾注释
            if c:
                rest = rest[:c.start()]
            out = []
            for tok in rest.split():
                d = tok.lower().rstrip(".")
                if not d or is_ip(d) or not is_valid_domain(d):
                    continue
                out.append(("domain", ("@@" if is_allow else "") + f"||{d}^"))
            return out
        # 5d) 纯域名（无协议、无路径、无符号）
        if (
            "/" not in line and ":" not in line and "#" not in line
            and DOMAIN_CHARS.match(line) and "." in line
        ):
            d = line.rstrip(".")
            if not is_ip(d) and is_valid_domain(d):
                return [("domain", ("@@" if is_allow else "") + f"||{d.lower()}^")]
        # 5e) @@domain / @@domain^ / @@domain$mod（无 || 前缀的白名单）归一化
        if is_allow:
            d = body
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            if d.endswith("^"):
                d = d[:-1]
            d = d.rstrip(".")
            if (
                "/" not in d and ":" not in d
                and DOMAIN_CHARS.match(d) and "." in d
                and not is_ip(d) and is_valid_domain(d)
                and (not dmods or check_modifiers(dmods))
            ):
                rule = f"@@||{d.lower()}^"
                if dmods:
                    rule += f"${normalize_mods(dmods)}"
                return [("domain", rule)]
        return []  # 其他杂项（带协议、带路径等）一律剔除

    # 6) ||domain 规则
    rest = body[2:]
    mods = ""
    if "$" in rest:
        rest, mods = rest.split("$", 1)
    if "/" in rest:          # 带路径 → DNS 层无效
        return []
    if ":" in rest:          # 带端口/IPv6 → 剔除
        return []
    if rest.endswith("^"):
        domain = rest[:-1]
    else:
        domain = rest
    domain = domain.lower().rstrip(".")   # 剥掉末尾 DNS 根点（||piwik.^ → ||piwik^）
    if not domain or not DOMAIN_CHARS.match(domain) or is_ip(domain) or not is_valid_domain(domain):
        return []
    if mods:
        if not check_modifiers(mods):
            # 白名单降级：@@||x^$domain=y / $third-party 这类，$domain/$third-party 是浏览器层概念，
            # DNS 层不认识。若整条丢弃，会导致"本该放行的域名没放行"，反而误拦截。
            # 这里剥掉 AGH 不支持的修饰符、保留合法子集；剥光就退化为裸域名放行。
            # 黑名单仍保守整条丢弃（避免把"仅第三方拦截"悄悄放宽成全局拦截）。
            if is_allow:
                kept = []
                for item in mods.split(","):
                    probe = _sanitize_mods(item)
                    if probe and check_modifiers(probe):
                        kept.append(probe)
                mods = ",".join(kept)
            else:
                return []

    rule = ("@@" if is_allow else "") + f"||{domain}^"
    if mods:
        rule += f"${normalize_mods(mods)}"
    return [("domain", rule)]


# ============================================================
# 白名单冲突处理
# ============================================================
def _has_modifier(rule: str, name: str) -> bool:
    """规则是否带某个无值修饰符（如 important），与修饰符书写顺序/组合无关。"""
    if "$" not in rule:
        return False
    return any(m.split("=", 1)[0].strip().lower() == name
               for m in rule.split("$", 1)[1].split(","))


def filter_conflicts(blacklist: list, allowlist: list, strict: bool = True) -> list:
    """
    白名单优先：剔除与白名单冲突的黑名单规则，并正确处理 $important 优先级。
    - 裸域白名单 @@||example.com^：放行 example.com 本身及其所有子域
    - 通配白名单 @@||*.example.com^：只放行其子域（不含裸域）
    - strict=False（--no-strict）：仅精确匹配，不排除子域
    - important 语义（AGH 官方）：||x^$important 拦截规则不向"普通白名单"让步，
      只有同样带 $important 的白名单 @@||x^$important 才能解除它；普通黑名单则向
      任意白名单（普通 + important）让步。
    算法：后缀剥离（对每个黑名单域名检查所有后缀是否命中白名单），O(n·域名段数)
    """
    def build(rules):
        exact, wild, pat = set(), set(), []
        for r in rules:
            if not r.startswith("@@"):
                continue
            d = extract_domain(r)
            if not d:
                continue
            if d.startswith("*."):
                wild.add(d[2:])
            elif "*" in d:
                # 中间通配白名单（如 a*.example.com）：预编译 glob 正则（* 跨段，符合 AGH 子域语义）
                pat.append(re.compile(fnmatch.translate(d)))
            else:
                exact.add(d)
        return exact, wild, pat

    ordinary = build([r for r in allowlist if not _has_modifier(r, "important")])
    powerful = build([r for r in allowlist if _has_modifier(r, "important")])

    def make_match(struct):
        exact, wild, pat = struct

        def is_conflict(dom: str) -> bool:
            if not strict:                 # 精确模式：仅完整域名相等
                return dom in exact
            parts = dom.split(".")
            for i in range(len(parts)):
                suffix = ".".join(parts[i:])
                if suffix in exact:
                    return True
                if suffix in wild and i > 0:   # 通配白名单只放行子域
                    return True
            # 中间通配模式：预编译正则匹配
            return any(p.match(dom) for p in pat)
        return is_conflict

    hit_ordinary = make_match(ordinary)
    hit_powerful = make_match(powerful)

    kept, removed, imp_kept, removed_regex = [], 0, 0, 0
    for rule in blacklist:
        # 正则拦截规则（/regex/）：按其能提取出的域名候选判断是否与白名单冲突
        if rule.startswith("/"):
            cands = regex_domain_candidates(rule)
            if not cands:
                kept.append(rule)            # 无法可靠提取域名，保守保留
                continue
            if _has_modifier(rule, "important"):
                conflict = any(hit_powerful(c) for c in cands)
            else:
                conflict = any(hit_ordinary(c) or hit_powerful(c) for c in cands)
            if conflict:
                removed += 1
                removed_regex += 1
            else:
                kept.append(rule)
            continue
        d = extract_domain(rule)
        if not d:
            kept.append(rule)
            continue
        if _has_modifier(rule, "important"):
            # important 拦截：仅 important 白名单可解除；普通白名单不能削弱
            conflict = hit_powerful(d)
            if not conflict:
                imp_kept += 1
        else:
            # 普通拦截：任意白名单（普通或 important）命中即放行
            conflict = hit_ordinary(d) or hit_powerful(d)
        if conflict:
            removed += 1
        else:
            kept.append(rule)
    log(f"白名单冲突剔除: {removed} 条黑名单规则（含正则 {removed_regex} 条，"
        f"{'严格' if strict else '精确'}匹配，"
        f"白名单域名 {sum(len(s[0]) + len(s[1]) + len(s[2]) for s in (ordinary, powerful))} 个，"
        f"保留 important 拦截 {imp_kept} 条）")
    return kept


def _rule_key(rule: str) -> str:
    """规则规范化键：域名部分原样（已小写），修饰符排序，用于 badfilter 精确配对，
    避免因修饰符书写顺序不同（important,badfilter vs badfilter,important）漏配。"""
    if "$" not in rule:
        return rule
    head, mods = rule.split("$", 1)
    items = sorted(m for m in mods.split(",") if m)
    return head + ("$" + ",".join(items) if items else "")


def apply_badfilter(rules: list):
    """
    解析 $badfilter（AdGuard/AGH 语法）：带该修饰符的规则不拦截，
    而是"禁用同列表内另一条去掉 badfilter 后完全相同的规则"。
    多源合并为同一个 AGH 订阅文件后，在构建期把这层语义解析掉：
      1. 收集所有 badfilter 指令，去掉 badfilter 修饰符、规范化后得到"目标规则键"
      2. 从普通规则中删除精确命中目标的条目
      3. badfilter 指令本身不写入产物（它是控制指令，不是拦截/放行规则）
    注意：badfilter 是精确规则配对（官方语义），不做域名通配，避免误删。
    返回 (保留规则 list, 被禁用数, 悬空指令数)。悬空=找不到对应规则（可能本就被其他清洗剔除）。
    """
    target_keys = set()
    normal = []
    for r in rules:
        mods = r.split("$", 1)[1] if "$" in r else ""
        if "badfilter" not in [m.strip().lower() for m in mods.split(",")]:
            normal.append(r)
            continue
        head = r.split("$", 1)[0]
        kept_mods = sorted(m for m in mods.split(",") if m and m.strip().lower() != "badfilter")
        target_keys.add(head + ("$" + ",".join(kept_mods) if kept_mods else ""))

    key_map = {}
    for r in normal:
        key_map.setdefault(_rule_key(r), r)
    kept, removed = [], 0
    for r in normal:
        if _rule_key(r) in target_keys:
            removed += 1
        else:
            kept.append(r)
    matched = sum(1 for k in target_keys if k in key_map)
    dangling = len(target_keys) - matched
    return kept, removed, dangling


def dedupe_covered_domains(rules: list, strict: bool = True):
    """
    父域覆盖去重（仅针对无修饰符规则；行为完全保持，不改变拦截/放行范围）。
    同时适用于黑名单（||）与白名单（@@||），两组按前缀独立去重、绝不互相覆盖：
      - ||example.com^ 已匹配 example.com 本身及其所有子域，
        则无修饰符的 ||a.example.com^ / ||a.b.example.com^ 永远不产生额外匹配，属冗余；
        白名单 @@||example.com^ 同理（已放行整个域，子域放行规则冗余）。
      - ||*.example.com^ 在 ||example.com^ 存在时冗余（裸域规则已覆盖全部子域）。
      - ||*.example.com^ 存在时，无修饰符的具体子域 ||a.example.com^ 冗余。
    一律保留（有独立语义，不能被无修饰符父域替代）：
      带任意修饰符（important/client/dnstype…）、中间通配（||a*.x^）、正则、单段规则。
    strict=False（--no-strict 精确匹配模式）时：白名单只精确放行裸域本身、不放行子域，
    此时子域白名单有独立作用，不得按父域覆盖去重；黑名单去重不受影响（||domain^ 的
    AGH 匹配语义与冲突模式无关，总是覆盖子域）。
    返回 (精简后规则 list, 移除数)。
    """
    # 按前缀分组：prefix -> [裸域 set, 左通配 base set]；拦截 || 与放行 @@|| 严格隔离
    groups, other = {}, []
    for r in rules:
        if r.startswith("@@||"):
            pre = "@@||"
            if not strict:
                other.append(r)          # 精确模式：父域白名单不放行子域，子域白名单不冗余
                continue
        elif r.startswith("||"):
            pre = "||"
        else:
            other.append(r)                # 正则等非域名规则：原样保留
            continue
        if "$" in r:
            other.append(r)                # 带修饰符：保留
            continue
        body = r[len(pre):]
        if body.endswith("^"):
            body = body[:-1]
        plain, leftwild = groups.setdefault(pre, [set(), set()])
        if "*" in body:
            if body.startswith("*."):
                leftwild.add(body[2:])     # 纯左通配 *.base
            else:
                other.append(r)            # 中间通配：保留
        elif "." in body:
            plain.add(body)                # 无修饰符裸域
        else:
            other.append(r)                # 单段规则：保留

    kept = list(other)
    removed = 0
    for pre, (plain, leftwild) in groups.items():
        def covered(dom: str, plain=plain, leftwild=leftwild) -> bool:
            parts = dom.split(".")
            for i in range(1, len(parts)):    # i>=1：只比对严格父后缀，绝不用自身/TLD
                suf = ".".join(parts[i:])
                if suf in plain or suf in leftwild:
                    return True
            return False

        for base in sorted(leftwild):         # 左通配：裸域存在、或其父域被覆盖即冗余
            if base in plain or covered(base):
                removed += 1
            else:
                kept.append(pre + "*." + base + "^")
        for dom in plain:                     # 裸域：被任一父域/左通配覆盖即冗余
            if covered(dom):
                removed += 1
            else:
                kept.append(pre + dom + "^")
    return kept, removed


# ============================================================
# 输出
# ============================================================
def header(title: str, count: int) -> str:
    now = datetime.datetime.now().astimezone(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    return (
        "! Title: " + title + "\n"
        "! Homepage: " + CONFIG["homepage"] + "\n"
        "! Version: " + now + "（北京时间）\n"
        "! Expires: 12 hours\n"
        "! Description: 由 sofmed DNS独家提供\n"
        f"! Total count: {count}\n"
    )


def sort_key(rule: str):
    imp = 0 if _has_modifier(rule, "important") else 1  # important 规则靠前
    return (imp, len(rule), rule)


def atomic_write(path: Path, text: str) -> None:
    """原子写入：先写同目录临时文件，再 os.replace 覆盖。
    构建中断/进程被杀不会留下半截文件，dist 产物始终保持完整可读。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_outputs(blacklist, allowlist, regex_rules, stats, hosts_source=None):
    DIST_DIR.mkdir(exist_ok=True)

    # 黑名单 = 域名规则 + 正则规则（blacklist 为父域覆盖去重后的精简集，供 AGH 原生规则使用）
    blk = sorted(set(blacklist), key=sort_key)
    regex = sorted(set(regex_rules), key=sort_key)
    all_blk = blk + regex
    alw = sorted(set(allowlist), key=sort_key)

    # 1) AGH 拦截规则
    atomic_write(CONFIG["out_rules"], header(CONFIG["title"], len(all_blk)) + "\n".join(all_blk) + "\n")
    log(f"✅ {CONFIG['out_rules'].name}: {len(all_blk)} 条")

    # 2) AGH 白名单
    atomic_write(CONFIG["out_allow"],
                 header(CONFIG["title"] + "（白名单）", len(alw)) + "\n".join(alw) + "\n")
    log(f"✅ {CONFIG['out_allow'].name}: {len(alw)} 条")

    # 3) hosts 格式（hosts 不支持通配符 * 与单段域名，需过滤；纯域名规则才可写）
    domains = set()
    if CONFIG["keep_hosts"]:
        # hosts 是无条件生效的：带"条件修饰符"（仅特定客户端/类型生效）的规则不能转成 hosts，
        # 否则会把"只在某客户端拦截"误变成"全局拦截"
        def has_cond_mod(r: str) -> bool:
            if "$" not in r:
                return False
            mods = {
                m.split("=", 1)[0].strip().lstrip("~").lower()
                for m in r.split("$", 1)[1].split(",") if m.strip()
            }
            return bool(mods & set(COND_MODS))
        # 关键：hosts 没有子域继承语义（0.0.0.0 example.com 不会屏蔽 a.example.com），
        # 因此 hosts 必须用"父域去重前"的完整规则集逐主机名枚举，不能用精简集，
        # 否则会漏掉被父域规则覆盖、但 hosts 仍需逐行列出的子域。
        hosts_blk = sorted(set(hosts_source if hosts_source is not None else blacklist),
                           key=sort_key)
        # hosts 标准主机名（RFC 952）不允许下划线，且下划线标签多为 TXT/服务发现记录，
        # 故 hosts 排除下划线域名；AGH 原生过滤文件（|| 语法，基于 DNS 标签）仍保留它们。
        domains = sorted({
            extract_domain(r) for r in hosts_blk
            if "*" not in r and "?" not in r and r.startswith("||")
            and "." in extract_domain(r) and "_" not in extract_domain(r)
            and not has_cond_mod(r)
        })
        atomic_write(CONFIG["out_hosts"],
                     "# Title: " + CONFIG["title"] + "（Hosts）\n"
                     + f"# Total count: {len(domains)}\n"
                     + "\n".join(f"0.0.0.0 {d}" for d in domains) + "\n")
        log(f"✅ {CONFIG['out_hosts'].name}: {len(domains)} 条")

    # 4) 统计 JSON
    stats["output"] = {
        "rules_file": str(CONFIG["out_rules"].relative_to(BASE_DIR)),
        "rules_count": len(all_blk),
        "allow_file": str(CONFIG["out_allow"].relative_to(BASE_DIR)),
        "allow_count": len(alw),
        "hosts_file": str(CONFIG["out_hosts"].relative_to(BASE_DIR)) if CONFIG["keep_hosts"] else None,
        "hosts_count": len(domains) if CONFIG["keep_hosts"] else 0,
    }
    atomic_write(CONFIG["out_stats"], json.dumps(stats, indent=2, ensure_ascii=False))
    log(f"✅ {CONFIG['out_stats'].name} 已写入")


# ============================================================
# 主流程
# ============================================================
def load_sources(path: Path) -> list:
    if not path.exists():
        log(f"✗ 找不到源文件: {path}")
        return []
    # 编码兜底：默认 utf-8；Windows/GBK 用户自定义源文件也能读
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gb18030", errors="replace")
    except Exception as e:
        log(f"✗ 读取源文件失败 {path}: {e}")
        return []
    if text and text[0] == "\ufeff":      # 去 UTF-8 BOM（用户自定义源文件可能带）
        text = text[1:]
    seen, out = set(), []
    bad = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        # 行尾注释：# 前至少有一个空白字符才视为注释（避免误伤 URL fragment，如 https://x.com/a#frag）
        m = re.search(r"\s+#", ln)
        if m:
            ln = ln[:m.start()].strip()
        if not ln.startswith(("http://", "https://", "file:")):
            # 源清单只允许 URL 或 file: 本地路径；误写规则行/其他内容直接警告跳过，
            # 避免被当作网络源反复下载失败（每次 3 次重试 + 第二轮补拉，拖慢构建数十秒）
            bad += 1
            continue
        if ln and ln not in seen:
            seen.add(ln)
            out.append(ln)
    if bad:
        log(f"⚠ 源清单跳过 {bad} 行非法条目（仅接受 http:// https:// file: 开头的行）")
    return out


def resolve_path(p: str) -> Path:
    """相对路径基于程序目录解析（与 file: 源一致），绝对路径原样使用"""
    path = Path(p)
    return path if path.is_absolute() else BASE_DIR / path


def process_source(source: str, stats: dict, is_allow_source: bool = False):
    """
    下载并清洗一个源，返回 (blacklist, allowlist, regex_rules, 统计)。
    is_allow_source=True 时（来自 allow-sources.txt）：
      - 源内所有有效规则都视为白名单（普通域名行自动加 @@ 前缀），
        避免"白名单源的普通规则被错误归入拦截列表"。
    is_allow_source=False（拦截源）：
      - 保留 @@ 前缀规则进白名单、普通规则进拦截（自带上游白名单规则）。
    """
    lines = fetch_source(source)
    ok = lines is not None          # None=网络失败待补拉；[]=成功但空/本地确定性失败
    blk, alw, reg = [], [], []
    s = {"total": 0, "kept": 0, "comment": 0, "element": 0, "js": 0,
         "path": 0, "modifier": 0, "ip": 0, "invalid": 0}
    for line in (lines or []):
        raw = line.strip()
        if not raw:
            continue
        s["total"] += 1
        if raw.startswith("!") or raw.startswith("#"):
            s["comment"] += 1
            continue
        if "##" in raw or "#@#" in raw:
            s["element"] += 1
            continue
        if "#%#" in raw or "#@%#" in raw:
            s["js"] += 1
            continue

        result = clean_rule(raw, CONFIG["keep_regex"])
        if not result:
            # 归纳剔除原因（仅供参考，按优先级归一类）
            body0 = raw[2:] if raw.startswith("@@") else raw
            host_part = raw.lstrip("@|").split("$", 1)[0].rstrip("^").split()
            host_part = host_part[-1] if host_part else ""
            if "/" in body0 and not body0.startswith("/"):
                s["path"] += 1
            elif "$" in raw and not check_modifiers(raw.split("$", 1)[1]):
                s["modifier"] += 1
            elif is_ip(host_part):
                s["ip"] += 1
            else:
                s["invalid"] += 1
            continue
        for kind, rule in result:
            s["kept"] += 1
            if kind == "regex":
                # 白名单正则（@@/regex/）进白名单；白名单源的普通正则自动加 @@ 前缀
                if rule.startswith("@@"):
                    alw.append(rule)
                elif is_allow_source:
                    alw.append("@@" + rule)
                else:
                    reg.append(rule)
            elif rule.startswith("@@") or is_allow_source:
                # 白名单源：普通规则也强制归入白名单（加 @@ 前缀）
                if is_allow_source and not rule.startswith("@@"):
                    rule = "@@" + rule
                alw.append(rule)
            else:
                blk.append(rule)

    stats["sources"][source] = {
        "is_allow_source": is_allow_source,
        "ok": ok,
        "lines": s["total"],
        "kept_black": len(blk),
        "kept_allow": len(alw),
        "kept_regex": len(reg),
        "skipped": s,
    }
    return blk, alw, reg, ok


def main():
    ap = argparse.ArgumentParser(description="AGH-Builder: 只适配 AdGuard Home 的规则合并器")
    ap.add_argument("--no-regex", action="store_true", help="不保留正则规则")
    ap.add_argument("--no-hosts", action="store_true", help="不生成 hosts 文件")
    ap.add_argument("--no-strict", action="store_true", help="白名单冲突仅精确匹配（默认含子域名）")
    ap.add_argument("--sources", default=None, help="自定义黑名单源文件")
    ap.add_argument("--allow-sources", default=None, help="自定义白名单源文件")
    args = ap.parse_args()

    if args.no_regex:
        CONFIG["keep_regex"] = False
    if args.no_hosts:
        CONFIG["keep_hosts"] = False
    if args.no_strict:
        CONFIG["strict_conflict"] = False

    black_sources = load_sources(resolve_path(args.sources) if args.sources else CONFIG["sources_file"])
    allow_sources = load_sources(resolve_path(args.allow_sources) if args.allow_sources else CONFIG["allow_sources_file"])
    log(f"拦截源 {len(black_sources)} 个，白名单源 {len(allow_sources)} 个，开始并发下载…")

    stats = {
        "builder": "AGH-Builder",
        "version": "2.0",
        "generated_at": datetime.datetime.now().astimezone(
            datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "sources": {},
        "config": {
            k: (str(v) if isinstance(v, Path) else v)
            for k, v in CONFIG.items() if k not in ("sources_file", "allow_sources_file")
        },
    }

    t0 = time.time()
    all_blk, all_alw, all_reg = [], [], []
    # 去重合并源列表：同一 URL 若同时出现在拦截与白名单列表（如茯苓白名单），
    # 只下载处理一次，避免重复下载、stats 源记录被覆盖、计数翻倍。
    black_set = set(black_sources)
    allow_only = [s for s in allow_sources if s not in black_set]
    merged = [(s, False) for s in black_sources] + [(s, True) for s in allow_only]
    log(f"去重后实际下载 {len(merged)} 个源（拦截 {len(black_sources)} + 仅白名单 {len(allow_only)}，"
        f"重复 {len(black_sources) + len(allow_sources) - len(merged)} 个）")

    def run_round(sources):
        """并发下载+清洗一批源，返回 (成功结果 dict{url:(blk,alw,reg)}, 失败 [(url,is_allow)])"""
        results, failed = {}, []
        with cf.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as ex:
            futures = {ex.submit(process_source, s, stats, is_allow): (s, is_allow)
                       for s, is_allow in sources}
            for fut in cf.as_completed(futures):
                s, is_allow = futures[fut]
                try:
                    blk, alw, reg, ok = fut.result()
                except Exception as e:
                    failed.append((s, is_allow))
                    log(f"✗ 处理源异常: {s}  ({str(e)[:100]})")
                    continue
                if ok:
                    results[s] = (blk, alw, reg)
                else:
                    failed.append((s, is_allow))
        return results, failed

    # 第一轮并发下载
    results, failed = run_round(merged)
    # 第二轮补拉：仅对网络失败源，等待一个退避窗口后整批重试
    # （连续重试常撞上同一波抖动；隔数十秒后网络往往已恢复）
    if failed:
        log(f"⚠ 第一轮 {len(failed)} 个源失败，等待 {CONFIG['retry_wait']}s 后第二轮补拉…")
        time.sleep(CONFIG["retry_wait"])
        results2, failed = run_round(failed)
        results.update(results2)
        if results2:
            log(f"✓ 第二轮补拉成功 {len(results2)} 个源")
    if failed:
        log(f"⚠ 最终仍有 {len(failed)} 个源失败（已跳过，不影响其他源）")

    # 按源清单顺序合并（保证跨平台构建结果确定性）
    for s, _ in merged:
        if s in results:
            blk, alw, reg = results[s]
            all_blk += blk
            all_alw += alw
            all_reg += reg

    log(f"下载+清洗完成，耗时 {time.time()-t0:.1f}s。"
        f"合并前：黑名单 {len(all_blk)} / 白名单 {len(all_alw)} / 正则 {len(all_reg)}")

    # 空结果保护：全部源失败/为空时，不覆盖已有产物
    if not all_blk and not all_alw and not all_reg:
        log("✗ 所有源均为空或全部失败，中止构建（保留 dist/ 现有产物）")
        return 1

    # 去重
    all_blk, all_alw, all_reg = set(all_blk), set(all_alw), set(all_reg)

    # 解析 $badfilter：黑名单/白名单分别在合并后的同一列表内禁用对应规则
    blk_kept, blk_bf, blk_dangle = apply_badfilter(list(all_blk))
    all_blk = set(blk_kept)
    alw_kept, alw_bf, alw_dangle = apply_badfilter(list(all_alw))
    all_alw = set(alw_kept)
    if blk_bf or alw_bf:
        log(f"badfilter 解析: 禁用黑名单 {blk_bf} 条、白名单 {alw_bf} 条（悬空指令 "
            f"{blk_dangle + alw_dangle} 条，无对应规则，已丢弃）")

    # 白名单冲突处理（--no-strict 时退化为精确匹配，仍生效）
    # 正则拦截规则也参与：把域名黑名单与正则黑名单一起过冲突，再按是否以 / 开头拆回两组
    _combined = list(all_blk) + list(all_reg)
    _kept = set(filter_conflicts(_combined, list(all_alw), strict=CONFIG["strict_conflict"]))
    all_blk = set(r for r in _kept if not r.startswith("/"))
    all_reg = set(r for r in _kept if r.startswith("/"))

    # hosts 用完整集（hosts 无子域继承，必须逐主机名枚举）；在父域去重前快照
    blk_for_hosts = set(all_blk)

    # 父域覆盖去重（无修饰符子域规则被父域规则完全覆盖，删除不改变 AGH 拦截范围）
    blk_dedup, cov_removed = dedupe_covered_domains(sorted(all_blk))
    all_blk = set(blk_dedup)
    # 白名单同样按子域继承去重（@@||example.com^ 已放行整个域，子域放行规则冗余）；
    # 放在黑名单冲突判断之后，用完整白名单做完冲突剔除再精简，放行范围不变。
    # --no-strict 精确模式下父域白名单不放行子域，子域白名单不冗余，跳过白名单去重。
    alw_dedup, alw_cov = dedupe_covered_domains(sorted(all_alw), strict=CONFIG["strict_conflict"])
    all_alw = set(alw_dedup)
    if cov_removed or alw_cov:
        log(f"父域覆盖去重: 黑名单精简 {cov_removed} 条、白名单精简 {alw_cov} 条冗余子域规则"
            f"（仅作用于 AGH 原生规则，匹配范围不变；hosts 仍逐主机名完整枚举）")

    stats["dedup"] = {
        "blacklist": len(all_blk),
        "allowlist": len(all_alw),
        "regex": len(all_reg),
        "total": len(all_blk) + len(all_alw) + len(all_reg),
    }

    write_outputs(all_blk, all_alw, all_reg, stats, hosts_source=blk_for_hosts)
    log("=" * 56)
    log(f"完成！最终规则: 拦截 {len(all_blk) + len(all_reg)} 条 + 白名单 {len(all_alw)} 条")
    log(f"输出目录: {DIST_DIR}")


if __name__ == "__main__":
    sys.exit(main() or 0)
