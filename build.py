#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGH-Builder v2.6 — 本程序适配 AdGuard Home。

v2.6 相对 v2.5 的修复（语义正确性 + 安全）：
  ✗ 修复裸域名 / hosts 行误扩子域：官方语义 example.org 与 0.0.0.0 example.org
      只匹配该主机名本身、不匹配子域；旧版写成 ||example.org^ 会扩大拦截到子域。
      现归一化为精确单主机 |example.org|。
  ✗ 修复单主机白名单误删域拦截规则：@@|host| 只放行 host 一个名字，
      不能用来删除还覆盖子域的 ||host^。host_exact 只对 |host| 单主机拦截规则生效。
  ✗ 修复正则冲突删除误删：新判定器要求正则以未转义 $ 结尾（匹配名必以 pattern 结尾，
      不会拼出 pattern.evil.com）、可靠解析字符类/分组（[.] 视为点）、
      且丢弃可被前缀改变的首标签后，保证后缀全部被白名单覆盖才删除。
  ✗ 安全加固：HTTP 重定向只允许 http/https，且重定向目标不得解析到内网/环回/链路本地地址。

v2.5 相对 v2.4 的修复：
  ✗ domain| 无边界后缀误配（example.com| 误配 notexample.com）归一化为 ||example.com^
  ✗ 正则白名单冲突从“一律保留”改为按可靠判定处理（v2.6 进一步收紧为零误删）

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
  ✓ hosts / 纯域名行 归一化为精确单主机 |domain|（官方语义不自动覆盖子域）

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
import io
import ipaddress
import json
import os
import random
import re
import socket
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit


# fetch_source 返回该哨兵表示“确定性失败”，不应进入第二轮网络重试。
FETCH_PERMANENT_FAILURE = object()

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
    "retry_backoff": 1.5,      # 单源内连续重试的退避基数：sleep(retry_backoff * (attempt+1))
    "max_source_bytes": 100 * 1024 * 1024,  # 单源下载上限（100MB），防异常响应撑爆内存
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (compatible; AGH-Builder/2.6)",
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
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# 布尔型修饰符（无值）
BOOL_MODS = {"important", "badfilter"}

# 当前 AdGuard DNS/AGH 文档所依据的 DNS RR 类型集合。
# 包含标准、历史及当前部署中常见的类型；ANY/AXFR/IXFR 等查询类型不作为 dnstype。
DNS_RR_TYPES = frozenset({
    "A", "A6", "AAAA", "AFSDB", "APL", "AVC", "CAA", "CDNSKEY", "CDS",
    "CERT", "CNAME", "CSYNC", "DHCID", "DLV", "DNAME", "DNSKEY", "DS",
    "DSYNC", "EUI48", "EUI64", "GPOS", "HINFO", "HIP", "HTTPS", "IPSECKEY",
    "ISDN", "KEY", "KX", "L32", "L64", "LOC", "LP", "MB", "MD", "MF",
    "MG", "MINFO", "MR", "MX", "NAPTR", "NID", "NINFO", "NS", "NSAP",
    "NSAP_PTR", "NSEC", "NSEC3", "NSEC3PARAM", "NULL", "NXT", "OPENPGPKEY",
    "OPT", "PTR", "PX", "RESINFO", "RP", "RRSIG", "RT", "SIG", "SMIMEA",
    "SOA", "SPF", "SRV", "SSHFP", "SVCB", "TA", "TKEY", "TLSA", "TSIG",
    "TXT", "UNSPEC", "URI", "WALLET", "WKS", "X25", "ZONEMD"
})

# 当前官方文档明确给出的 dnsrewrite 空响应关键字；关键词必须大写。
DNS_REWRITE_RCODE = frozenset({"NOERROR", "NXDOMAIN", "SERVFAIL", "REFUSED"})

# 当前官方文档列出的 dnsrewrite RR 类型。
DNS_REWRITE_RR_TYPES = frozenset({"A", "AAAA", "CNAME", "HTTPS", "MX", "PTR", "SVCB", "SRV", "TXT"})

AGH_CTAG_VALUES = frozenset({
    "device_audio", "device_camera", "device_gameconsole", "device_laptop",
    "device_nas", "device_pc", "device_phone", "device_printer",
    "device_securityalarm", "device_tablet", "device_tv", "device_other",
    "os_android", "os_ios", "os_linux", "os_macos", "os_windows", "os_other",
    "user_admin", "user_regular", "user_child",
})
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
    """提取规则主体中的域名/主机名，兼容 ||、|host|、|host^、domain|。"""
    r = rule
    if r.startswith("@@"):
        r = r[2:]
    if r.startswith("||"):
        r = r[2:]
    elif r.startswith("|"):
        r = r[1:]
    if "$" in r:
        r = r.split("$", 1)[0]
    if "^" in r:
        r = r.split("^", 1)[0]
    if r.endswith("|"):
        r = r[:-1]
    if "/" in r:
        r = r.split("/", 1)[0]
    return r.lower().rstrip(".")


# ============================================================
# 正则规则安全删除判定（保证零误删）
# ============================================================
def _regex_alternations(pattern: str):
    """按顶层（分组外、字符类外、未转义）| 拆分交替分支；结构不可靠返回 None。"""
    branches, buf = [], []
    depth = 0
    in_class = False
    escaped = False
    for ch in pattern:
        if escaped:
            buf.append(ch); escaped = False; continue
        if ch == "\\":
            buf.append(ch); escaped = True; continue
        if in_class:
            buf.append(ch)
            if ch == "]":
                in_class = False
            continue
        if ch == "[":
            buf.append(ch); in_class = True; continue
        if ch == "(":
            depth += 1; buf.append(ch); continue
        if ch == ")":
            depth -= 1
            if depth < 0:
                return None
            buf.append(ch); continue
        if ch == "|" and depth == 0:
            branches.append("".join(buf)); buf = []; continue
        buf.append(ch)
    if escaped or in_class or depth != 0:
        return None
    branches.append("".join(buf))
    return branches


def _skip_quantifier(s: str, i: int) -> int:
    n = len(s)
    if i < n and s[i] in "*+?":
        return i + 1
    if i < n and s[i] == "{":
        j = s.find("}", i + 1)
        if j != -1:
            return j + 1
    return i


def _analyze_class_content(content: list):
    """分析字符类内容，返回 (only_dot, has_dot, has_other)。
    only_dot: 类只匹配点；has_dot: 类可匹配点；has_other: 类可匹配非点。"""
    has_dot, has_other = False, False
    j, m = 0, len(content)
    while j < m:
        c = content[j]
        if c == "\\" and j + 1 < m:
            nx = content[j + 1]
            if nx == ".":
                has_dot = True
            else:
                has_other = True
            j += 2; continue
        if c == ".":
            has_dot = True
            j += 1; continue
        has_other = True
        j += 1
    return (has_dot and not has_other), has_dot, has_other


def _scan_class(s: str, i: int):
    """从 s[i]=='[' 扫描字符类，返回 (content, 关闭]位置)；未闭合返回 (None, -1)。"""
    n = len(s)
    k = i + 1
    esc = False
    content = []
    while k < n:
        c = s[k]
        if esc:
            content.append(c); esc = False; k += 1; continue
        if c == "\\":
            content.append("\\"); esc = True; k += 1; continue
        if c == "]" and k > i + 1:
            return content, k
        content.append(c); k += 1
    return None, -1


def _group_can_span_dot(inner: str) -> bool:
    """分组内部是否可能匹配到点（真实点/裸点/含点字符类/大写通配 \\D \\W \\S）。"""
    j, n = 0, len(inner)
    escaped = False
    in_class = False
    while j < n:
        c = inner[j]
        if escaped:
            escaped = False; j += 1; continue
        if in_class:
            if c == "]":
                in_class = False
            j += 1; continue
        if c == "\\":
            if j + 1 < n:
                nx = inner[j + 1]
                if nx == "." or nx in "DWS":
                    return True
            escaped = True; j += 1; continue
        if c == "[":
            content, close = _scan_class(inner, j)
            if close < 0:
                return True
            _, has_dot, _ = _analyze_class_content(content)
            if has_dot:
                return True
            in_class = True
            j = close + 1; continue
        if c == ".":
            return True
        j += 1
    return False


class _BranchScanner:
    def __init__(self):
        self.labels = []
        self.lit = []
        self.wild = False
    def dot(self):
        if self.lit or self.wild:
            self.labels.append((not self.wild, "".join(self.lit)))
        self.lit, self.wild = [], False
    def finish(self):
        self.dot()
        return self.labels


def _regex_branch_labels(branch: str):
    """把一个交替分支解析为真实点分隔的标签序列 [(is_literal, text), ...]。
    标签内通配（\\d/\\w/\\s/不含点字符类/无点分组）-> (False, '')。
    遇到可匹配点的结构（裸点/\\D/\\W/\\S/含点字符类/含点分组）返回 None。"""
    sc = _BranchScanner()
    i, n = 0, len(branch)
    while i < n:
        ch = branch[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            nx = branch[i + 1]
            if nx == ".":
                sc.dot(); i += 2; continue
            if nx in "dws":
                sc.wild = True; i += 2; continue
            if nx in "DWS":
                return None
            sc.lit.append(nx); i += 2; continue
        if ch == "[":
            content, close = _scan_class(branch, i)
            if close < 0:
                return None
            only_dot, has_dot, _ = _analyze_class_content(content)
            k = _skip_quantifier(branch, close + 1)
            if only_dot:
                sc.dot()
            elif has_dot:
                return None
            else:
                sc.wild = True
            i = k; continue
        if ch == ".":
            return None
        if ch == "(":
            depth = 1; k = i + 1; esc2 = False; in_cls = False
            while k < n and depth > 0:
                c2 = branch[k]
                if esc2:
                    esc2 = False; k += 1; continue
                if in_cls:
                    if c2 == "]":
                        in_cls = False
                    k += 1; continue
                if c2 == "\\":
                    esc2 = True; k += 1; continue
                if c2 == "[":
                    in_cls = True; k += 1; continue
                if c2 == "(":
                    depth += 1
                elif c2 == ")":
                    depth -= 1
                k += 1
            if depth != 0:
                return None
            inner = branch[i + 1:k - 1]
            if _group_can_span_dot(inner):
                return None
            sc.wild = True
            i = _skip_quantifier(branch, k); continue
        if ch in "^$":
            i += 1; continue
        if ch in "*+?":
            i += 1; continue
        if ch == "{":
            j = branch.find("}", i + 1)
            if j == -1:
                return None
            i = j + 1; continue
        sc.lit.append(ch); i += 1
    return sc.finish()


def _regex_safe_to_remove(rule: str, ordinary, powerful, strict: bool) -> bool:
    """判定一条正则拦截规则能否在构建期安全删除（其全部匹配名都已被白名单覆盖）。"""
    head, mods = _parse_rule_modifiers(rule)
    if mods is None or not (head.startswith("/") and head.endswith("/") and len(head) >= 2):
        return False
    pattern = head[1:-1]
    # 必须以未转义 $ 结尾：保证匹配的主机名以 pattern 结尾，
    # 不会出现无结尾锚点的正则匹配 "pattern.evil.com" 的情况。
    last_dollar, escaped = -1, False
    for idx, ch in enumerate(pattern):
        if escaped:
            escaped = False; continue
        if ch == "\\":
            escaped = True; continue
        if ch == "$":
            last_dollar = idx
    if last_dollar != len(pattern) - 1:
        return False
    body = pattern[:-1]
    anchored_start = body.startswith("^")
    if anchored_start:
        body = body[1:]
    branches = _regex_alternations(body)
    if branches is None:
        return False
    names = {m.lstrip("~").split("=", 1)[0].strip().lower() for m in mods}
    is_imp = "important" in names
    for br in branches:
        labels = _regex_branch_labels(br)
        if labels is None:
            return False
        if not anchored_start:
            # 分支不以 ^ 开头时，前缀可拼接改变第一个标签，丢弃首标签。
            labels = labels[1:]
        # 从右向左收集连续字面量标签构成保证的核心域名；通配标签不跨越点，停止收集。
        core_parts = []
        for is_lit, txt in reversed(labels):
            if is_lit:
                core_parts.append(txt)
            else:
                break
        core_parts.reverse()
        if len(core_parts) < 2:          # 核心域名必须含至少一个真实点
            return False
        core = ".".join(core_parts).lower()
        # 单主机白名单（host_exact）不参与：正则可能匹配核心域的子域，单主机放行不足以覆盖。
        if is_imp:
            ok = _domain_matches(powerful, core, strict=strict, allow_host_exact=False)
        else:
            ok = (_domain_matches(ordinary, core, strict=strict, allow_host_exact=False)
                  or _domain_matches(powerful, core, strict=strict, allow_host_exact=False))
        if not ok:
            return False
    return True


# ============================================================
# 下载
# ============================================================
class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向安全加固：重定向目标仅允许 http/https，且 host 不得解析到
    内网 / 环回 / 链路本地 / 多播 / 保留 / 未指定地址（防 SSRF 与 file: 跳转）。
    初始 URL 不做此限制（用户可能订阅内网源）；仅源服务器主动发起的重定向受约束。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            try:
                fp.read()
            except Exception:
                pass
            raise HTTPError(req.full_url, code,
                            f"拒绝重定向到非 http(s) 地址: {newurl}", headers, fp)
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    raise ValueError("内网/保留地址")
        except (socket.gaierror, ValueError) as e:
            try:
                fp.read()
            except Exception:
                pass
            raise HTTPError(req.full_url, code,
                            f"拒绝重定向到不可信地址 {newurl}: {e}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def fetch_source(source: str):
    """
    读取一个源：file: 本地文件 或 http(s) 网络地址，返回按行分割的文本。
    返回值约定：
      - list  读取成功（含空内容）
      - FETCH_PERMANENT_FAILURE 确定性失败，不参与第二轮补拉
      - None  网络源重试耗尽（临时性失败，可做第二轮补拉）
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
                return FETCH_PERMANENT_FAILURE
            if not full.exists():
                log(f"  ✗ 本地文件不存在: {source}")
                return FETCH_PERMANENT_FAILURE
            if not full.is_file():
                log(f"  ✗ 本地源不是普通文件: {source}")
                return FETCH_PERMANENT_FAILURE
            size = full.stat().st_size
            if size > CONFIG["max_source_bytes"]:
                log(f"  ✗ 本地源过大（>{CONFIG['max_source_bytes'] / 1024 / 1024:.1f}MiB），已拒绝: {source}")
                return FETCH_PERMANENT_FAILURE
            return decode_text(full.read_bytes()).splitlines()
        except Exception as e:
            log(f"  ✗ 读取本地文件失败 {source}: {e}")
            return FETCH_PERMANENT_FAILURE

    for attempt in range(CONFIG["retries"]):
        try:
            req = urllib.request.Request(
                source, headers={
                    "User-Agent": random.choice(CONFIG["user_agents"]),
                    "Accept-Encoding": "gzip, deflate",
                }
            )
            with _OPENER.open(req, timeout=CONFIG["timeout"]) as resp:
                # 先限制压缩后的网络响应大小，再限制解压后的大小。
                # 两层限制同时防止普通大响应和 gzip/deflate 压缩炸弹撑爆内存。
                chunks, total = [], 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > CONFIG["max_source_bytes"]:
                        log(f"  ✗ 源过大（>{CONFIG['max_source_bytes'] / 1024 / 1024:.1f}MiB 压缩数据），已拒绝: {source}")
                        return FETCH_PERMANENT_FAILURE
                    chunks.append(chunk)
                raw = b"".join(chunks)
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                try:
                    raw = decompress_limited(raw, enc, CONFIG["max_source_bytes"])
                except ValueError as e:
                    log(f"  ✗ 解压后源过大或压缩数据损坏，已拒绝: {source} ({e})")
                    return FETCH_PERMANENT_FAILURE
            return decode_text(raw).splitlines()
        except HTTPError as e:
            # 4xx（除 429 限流）是永久性错误，重试与第二轮补拉都无意义，直接放弃（不补拉）
            if 400 <= e.code < 500 and e.code != 429:
                log(f"  ✗ 下载失败（HTTP {e.code}，永久错误不重试）: {source}")
                return FETCH_PERMANENT_FAILURE
            if attempt >= CONFIG["retries"] - 1:
                log(f"  ✗ 下载失败（{CONFIG['retries']} 次）: {source}  (HTTP {e.code})")
                return None
            time.sleep(CONFIG["retry_backoff"] * (attempt + 1))
        except Exception as e:
            if attempt >= CONFIG["retries"] - 1:
                log(f"  ✗ 下载失败（{CONFIG['retries']} 次）: {source}  ({str(e)[:80]})")
                return None
            time.sleep(CONFIG["retry_backoff"] * (attempt + 1))
    return None


def decompress_limited(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    """按流式方式解压 HTTP 响应，并限制解压后的总字节数。"""
    enc = (encoding or "").split(",", 1)[0].strip().lower()
    if enc in ("", "identity"):
        if len(raw) > max_bytes:
            raise ValueError(f">{max_bytes // 1024 // 1024}MB")
        return raw

    chunks = []
    total = 0

    def add_chunk(chunk: bytes) -> None:
        nonlocal total
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f">{max_bytes // 1024 // 1024}MB 解压数据")
        chunks.append(chunk)

    if enc == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
            while True:
                chunk = gz.read(64 * 1024)
                if not chunk:
                    break
                add_chunk(chunk)
        return b"".join(chunks)

    if enc == "deflate":
        def inflate(wbits: int) -> bytes:
            dec = zlib.decompressobj(wbits)
            local = []
            local_total = 0
            pos = 0
            while pos < len(raw):
                pending = raw[pos:pos + 64 * 1024]
                pos += len(pending)
                while pending:
                    remaining = max_bytes - local_total
                    if remaining <= 0:
                        raise ValueError(f">{max_bytes // 1024 // 1024}MB 解压数据")
                    out = dec.decompress(pending, remaining + 1)
                    if out:
                        local_total += len(out)
                        if local_total > max_bytes:
                            raise ValueError(f">{max_bytes // 1024 // 1024}MB 解压数据")
                        local.append(out)
                    pending = dec.unconsumed_tail
                    if pending and local_total >= max_bytes:
                        raise ValueError(f">{max_bytes // 1024 // 1024}MB 解压数据")
            tail = dec.flush(max_bytes - local_total + 1)
            if tail:
                local_total += len(tail)
                if local_total > max_bytes:
                    raise ValueError(f">{max_bytes // 1024 // 1024}MB 解压数据")
                local.append(tail)
            return b"".join(local)

        try:
            return inflate(zlib.MAX_WBITS)
        except zlib.error:
            return inflate(-zlib.MAX_WBITS)

    raise ValueError(f"不支持 Content-Encoding={enc!r}")


# ============================================================
# AGH 兼容性清洗（核心）
# ============================================================
# 修饰符值的合法字符（普通 modifier）。client/dnstype/ctag/denyallow 单独解析。
MOD_VALUE_CHARS = re.compile(r"^[a-zA-Z0-9_.:|\-/~;*]*$")
CLIENT_ATOM_CHARS = re.compile(r"^[a-zA-Z0-9_.:\-/]+$")
HOSTS_DOMAIN_CHARS = re.compile(
    r"^[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _split_escaped(value: str, sep: str):
    """按未转义分隔符切分，支持反斜杠转义；遇到悬空转义返回 None。"""
    out, buf = [], []
    escaped = False
    quote = None
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == sep:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped or quote is not None:
        return None
    out.append("".join(buf))
    return out


def _split_modifier_items(mods: str):
    """按逗号切分 modifier，同时尊重引号和反斜杠转义。"""
    parts = _split_escaped(mods, ",")
    return parts if parts is not None else [mods]


def _is_valid_quoted_client(value: str) -> bool:
    """验证单个 '...' / \"...\" client 值，允许转义引号、逗号和竖线。"""
    if len(value) < 2 or value[0] not in ("'", '"') or value[-1] != value[0]:
        return False
    quote = value[0]
    escaped = False
    for ch in value[1:-1]:
        if escaped:
            if ch not in ("'", '"', ",", "|", "\\"):
                return False
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            return False
    return not escaped


def _split_client_values(value: str):
    """按未转义、且位于引号外的 | 切分 client 列表。"""
    out, buf = [], []
    quote = None
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "|":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped or quote is not None:
        return None
    out.append("".join(buf))
    return out


def _validate_client_value(value: str) -> bool:
    values = _split_client_values(value)
    if not values or any(v == "" for v in values):
        return False
    for raw in values:
        v = raw[1:] if raw.startswith("~") else raw
        if not v:
            return False
        if v.startswith(("'", '"')):
            if not _is_valid_quoted_client(v):
                return False
            continue
        if not CLIENT_ATOM_CHARS.fullmatch(v):
            return False
        # If the value looks like an address/CIDR, validate it semantically rather than
        # merely accepting digit/dot/slash characters. Simple unquoted client names remain allowed.
        looks_like_address = (":" in v or "/" in v or bool(re.fullmatch(r"\d+(?:\.\d+){3}(?:/\d+)?", v)))
        if looks_like_address:
            try:
                if "/" in v:
                    ipaddress.ip_network(v, strict=False)
                else:
                    ipaddress.ip_address(v)
            except ValueError:
                return False
    return True


def _split_pipe_values(value: str):
    parts = _split_escaped(value, "|")
    if parts is None or not parts or any(not p for p in parts):
        return None
    return parts


def _is_valid_modifier_domain(value: str, allow_single: bool = True) -> bool:
    """验证 modifier 中的 DNS 名称。允许单标签，不允许通配符、路径、端口。"""
    d = value.rstrip(".").lower()
    if not d or "*" in d or "?" in d or "/" in d or ":" in d:
        return False
    if len(d) > 253 or not DOMAIN_CHARS.fullmatch(d):
        return False
    if not allow_single and "." not in d:
        return False
    for part in d.split("."):
        if not part or len(part) > 63 or part.startswith("-") or part.endswith("-"):
            return False
        # DNS service labels may legitimately contain underscores (e.g. _tcp), so
        # keep underscore support here; modifier semantics are DNS names, not /etc/hosts names.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
            return False
    return True


def _validate_dnstype(value: str) -> bool:
    values = _split_pipe_values(value)
    if values is None:
        return False
    for raw in values:
        token = raw[1:] if raw.startswith("~") else raw
        token = token.upper()
        if not token or token not in DNS_RR_TYPES:
            return False
    # 官方文档说明混合写法等价于保留 inclusion 项；AGH 可接受，因此不因该写法误删。
    return True


def _validate_ctag(value: str) -> bool:
    values = _split_pipe_values(value)
    if values is None:
        return False
    kinds = set()
    for raw in values:
        neg = raw.startswith("~")
        token = raw[1:] if neg else raw
        if token.lower() not in AGH_CTAG_VALUES:
            return False
        kinds.add(neg)
    return len(kinds) <= 1


def _validate_denyallow(value: str) -> bool:
    values = _split_pipe_values(value)
    if values is None:
        return False
    return all(_is_valid_modifier_domain(v, allow_single=True) for v in values)


def _validate_dnsrewrite_shorthand(value: str) -> bool:
    if value in DNS_REWRITE_RCODE:
        return True
    try:
        ip = ipaddress.ip_address(value)
        return ip.version in (4, 6)
    except ValueError:
        return _is_valid_modifier_domain(value, allow_single=True)


def _validate_dnsrewrite_full(value: str) -> bool:
    parts = value.split(";", 2)
    if len(parts) != 3:
        return False
    rcode, rr, data = parts
    if rcode not in DNS_REWRITE_RCODE:
        return False
    # Empty RR/VALUE is the empty-response form; documented as valid for NOERROR
    # and as RCODE;; for error responses.
    if not rr and not data:
        return True
    if not rr or rr not in DNS_REWRITE_RR_TYPES or not data:
        return False
    if rr == "A":
        try:
            return ipaddress.ip_address(data).version == 4
        except ValueError:
            return False
    if rr == "AAAA":
        try:
            return ipaddress.ip_address(data).version == 6
        except ValueError:
            return False
    if rr in {"CNAME", "PTR"}:
        target = data.rstrip(".")
        return data == "." or _is_valid_modifier_domain(target, allow_single=True)
    if rr == "MX":
        m = re.fullmatch(r"(\d{1,5})[ \t]+(.+)", data)
        if not m:
            return False
        priority, target = int(m.group(1)), m.group(2).rstrip(".")
        return priority <= 65535 and (target == "." or _is_valid_modifier_domain(target, allow_single=True))
    if rr == "SRV":
        m = re.fullmatch(r"(\d{1,5})[ \t]+(\d{1,5})[ \t]+(\d{1,5})[ \t]+(.+)", data)
        if not m:
            return False
        priority, weight, port = (int(m.group(i)) for i in (1, 2, 3))
        target = m.group(4).rstrip(".")
        return all(x <= 65535 for x in (priority, weight, port)) and (
            target == "." or _is_valid_modifier_domain(target, allow_single=True))
    if rr in {"HTTPS", "SVCB"}:
        toks = data.split()
        if len(toks) < 2 or not re.fullmatch(r"\d+", toks[0]) or int(toks[0]) > 65535:
            return False
        target = toks[1].rstrip(".")
        if target != "." and not _is_valid_modifier_domain(target, allow_single=True):
            return False
        for param in toks[2:]:
            if any(c in param for c in ('"', "\n", "\r", "\x00", "\x01", "\x02", "\x03", "\x04", "\x05", "\x06", "\x07", "\x08", "\x09")):
                return False
            if "=" not in param:
                return False
            key, val = param.split("=", 1)
            if not re.fullmatch(r"[A-Za-z0-9-]+", key) or not val or "," in val:
                return False
            if key.lower() == "ipv4hint":
                try:
                    if ipaddress.ip_address(val).version != 4:
                        return False
                except ValueError:
                    return False
            elif key.lower() == "ipv6hint":
                try:
                    if ipaddress.ip_address(val).version != 6:
                        return False
                except ValueError:
                    return False
        return True
    if rr == "TXT":
        return not any(c in data for c in "\x00\n\r^")
    return False

def _validate_dnsrewrite(value: str) -> bool:
    if not value or "\n" in value or "\r" in value or "^" in value:
        return False
    # 分号存在时必须是完整 RCODE;RR;VALUE 形式。
    return _validate_dnsrewrite_full(value) if ";" in value else _validate_dnsrewrite_shorthand(value)


def _sanitize_mods(mods: str) -> str:
    """仅清理首尾空白；绝不静默删除 modifier 值末尾的 ``^``。"""
    # ``^`` 在 modifier 值里可能是数据（尤其是 TXT dnsrewrite）。
    # 旧版无条件剥掉末尾 ``^`` 会把合法值静默改写，也会掩盖真正的格式错误。
    return mods.strip()


def check_modifiers(mods: str, is_exception: bool = False) -> bool:
    """严格校验 AGH 支持的 modifier；未知、不支持或参数语义不合法时整条规则无效。"""
    mods = _sanitize_mods(mods)
    if not mods:
        return True
    seen = set()
    items = _split_modifier_items(mods)
    if not items or any(not item.strip() for item in items):
        return False
    for raw in items:
        raw = raw.strip()
        negated = raw.startswith("~")
        item = raw[1:] if negated else raw
        name, sep, value = item.partition("=")
        name = name.lower().strip()
        if not name or name in seen:
            return False
        seen.add(name)
        if name in UNSUPPORTED_MODIFIERS or name not in SUPPORTED_MODIFIERS:
            return False
        # AGH 目前支持的否定写法位于 modifier 的值中，例如
        # client=~PC、dnstype=~A、ctag=~device_phone；不要把 ~ 放到 modifier 名之前。
        if negated:
            return False
        if not sep:
            if name not in BOOL_MODS and not (is_exception and name == "dnsrewrite"):
                return False
            if negated:
                return False
            continue
        if name in BOOL_MODS or not value:
            return False
        if name == "client":
            # 对 client 列表，~ 必须位于每个值的引号外；quoted client 自己可含转义 |/，
            if not _validate_client_value(value):
                return False
            continue
        if name == "dnstype":
            if negated:
                return False  # ~ 已经用于列表元素，不是 modifier 名本身
            if not _validate_dnstype(value):
                return False
            continue
        if name == "ctag":
            if negated:
                return False
            if not _validate_ctag(value):
                return False
            continue
        if name == "denyallow":
            if negated:
                return False
            if not _validate_denyallow(value):
                return False
            continue
        if name == "dnsrewrite":
            if negated:
                return False
            if not _validate_dnsrewrite(value):
                return False
            continue
        if not MOD_VALUE_CHARS.fullmatch(value):
            return False
    return True


def normalize_mods(mods: str) -> str:
    """归一化 modifier 名称；client/ dnsrewrite 值保留原样，dnstype/ctag/denyallow 按语义规范化。"""
    parts = []
    for item in _split_modifier_items(_sanitize_mods(mods)):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            parts.append(name.lower())
            continue
        nm = name.lstrip("~").lower()
        lname = name.lower()
        if nm in {"dnsrewrite", "client"}:
            parts.append(f"{lname}={value}")
        elif nm in {"dnstype", "ctag", "denyallow"}:
            values = _split_pipe_values(value)
            if values is not None:
                vals = []
                negatives = []
                positives = []
                for v in values:
                    token = v.lstrip("~")
                    if nm == "dnstype":
                        token = token.upper()
                    else:
                        token = token.lower()
                    (negatives if v.startswith("~") else positives).append(token)
                if nm == "dnstype" and positives:
                    # ~A|AAAA is semantically equivalent to AAAA; emit the canonical form.
                    vals = positives
                else:
                    vals = [("~" + x) for x in negatives] if negatives else positives
                value = "|".join(vals)
            parts.append(f"{lname}={value}")
        else:
            parts.append(f"{lname}={value.lower()}")
    return ",".join(parts)


def regex_is_agh_compatible(pattern: str) -> bool:
    """尽早拒绝明显不是 Go/RE2 风格正则的表达式，避免把无法编译的 regex 写入产物。"""
    if not isinstance(pattern, str) or not pattern.startswith("/") or len(pattern) < 2:
        return False
    escaped = False
    body = None
    for i in range(1, len(pattern)):
        ch = pattern[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "/":
            body = pattern[1:i]
            break
    if body is None or escaped:
        return False
    # RE2 does not implement lookaround, backreferences, recursion, or atomic groups.
    if re.search(r"\(\?(?:[=!]|<[=!]|P=|R|[0-9]|>)", body):
        return False
    # Detect an unescaped backreference (odd number of consecutive backslashes before digit).
    slash_run = 0
    for ch in body:
        if ch == "\\":
            slash_run += 1
            continue
        if ch.isdigit() and 1 <= slash_run % 2 and ch != "0":
            return False
        slash_run = 0
    try:
        re.compile(body)
    except re.error:
        return False
    return True


def split_regex_mods(inner: str, is_exception: bool = False):
    """按未转义关闭 / 分割正则主体和 modifier，并严格校验尾部。"""
    if not inner.startswith("/"):
        return inner, False
    escaped = False
    for i in range(1, len(inner)):
        ch = inner[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch != "/":
            continue
        head = inner[:i + 1]
        tail = inner[i + 1:]
        if not tail:
            return head, ""
        if not tail.startswith("$"):
            return head, False
        mods = _sanitize_mods(tail[1:])
        if not mods:
            return head, ""
        if check_modifiers(mods, is_exception):
            return head, mods
        return head, False
    return inner, False


def _parse_rule_modifiers(rule: str):
    """返回 (无 modifier 的主体, modifier 列表)。对 regex 正确识别第一个未转义关闭 /。"""
    prefix = "@@" if rule.startswith("@@") else ""
    inner = rule[2:] if prefix else rule
    if inner.startswith("/"):
        body_re, mods = split_regex_mods(inner, bool(prefix))
        if mods is False:
            return rule, None
        if mods is None:
            return rule, []
        return prefix + body_re, [m.strip() for m in _split_modifier_items(mods) if m.strip()]
    if "$" not in rule:
        return rule, []
    head, mods = rule.split("$", 1)
    return head, [m.strip() for m in _split_modifier_items(mods) if m.strip()]


def _modifier_items(rule: str):
    _, mods = _parse_rule_modifiers(rule)
    return [] if mods is None else mods


def _modifier_values(rule: str, target: str):
    out = []
    for raw in _modifier_items(rule):
        negated = raw.startswith("~")
        item = raw[1:] if negated else raw
        name, sep, value = item.partition("=")
        if name.lower().strip() == target.lower():
            out.append((negated, sep, value))
    return out


def _has_modifier(rule: str, name: str) -> bool:
    return name.lower() in _rule_modifier_names(rule)


def _rule_modifier_names(rule: str) -> set:
    return {
        raw.lstrip("~").split("=", 1)[0].strip().lower()
        for raw in _modifier_items(rule) if raw.strip()
    }


def _rule_key(rule: str) -> str:
    """规则规范化键；modifier 顺序不影响 badfilter 精确配对。"""
    head, mods = _parse_rule_modifiers(rule)
    if mods is None or not mods:
        return head
    canonical = [normalize_mods(m) for m in mods if m]
    return head + "$" + ",".join(sorted(canonical))


def is_valid_hosts_hostname(domain: str) -> bool:
    """严格验证 /etc/hosts 主机名；按 AGH 文档仅允许 ASCII 字母、数字、连字符和点。"""
    d = domain.rstrip(".")
    if not d or len(d) > 253:
        return False
    if d.lower() in RESERVED_NAMES or d.lower().endswith(".localhost"):
        return False
    if not HOSTS_DOMAIN_CHARS.fullmatch(d):
        return False
    return True


def strip_inline_comment(line: str) -> str:
    """删除空白后的 # 行尾注释，不误删 URL fragment、正则和元素规则。"""
    quote = None
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#":
            return line[:i].rstrip()
    return line.rstrip()


def clean_rule(line: str, keep_regex: bool):
    """清洗单行规则，返回 [(kind, rule), ...]；不符合 AGH DNS 规则的输入返回空列表。"""
    line = line.strip()
    if not line:
        return []
    if line.startswith("‖"):
        line = "||" + line[1:]
    # 正则中的 # 可以是字面量；行尾注释只对非 regex 规则应用。
    if line.startswith("!"):
        return []
    # 元素隐藏 / JS 注入必须在 inline comment 处理之前识别，避免 ``##`` 被截断后误当域名。
    if "##" in line or "#@#" in line or "#%#" in line or "#@%#" in line:
        return []
    # 正则独立解析；其他规则仅在没有 modifier 时按 hosts/domains-only 约定处理行尾注释。
    # 含 ``$`` 的规则保留 # 给 modifier parser，避免损坏 dnsrewrite/TXT 等值。
    if not line.startswith("/") and not line.startswith("@@/") and "$" not in line:
        line = strip_inline_comment(line)
        if not line:
            return []
    if line.startswith("#"):
        return []

    # 正则 /.../ 或 @@/.../；regex 内部的 $ 不当作 modifier 分隔符。
    inner = line[2:] if line.startswith("@@") else line
    if line.startswith("/") or line.startswith("@@/"):
        if not keep_regex:
            return []
        prefix = "@@" if line.startswith("@@/") else ""
        body_re, mods = split_regex_mods(inner, line.startswith("@@"))
        if mods is False or not regex_is_agh_compatible(body_re):
            return []
        inner = body_re if not mods else body_re + "$" + normalize_mods(mods)
        return [("regex", prefix + inner)]

    is_allow = line.startswith("@@")
    body = line[2:] if is_allow else line

    if not body.startswith("||"):
        # 全局掩码 *$mods
        if body.startswith("*"):
            gh = body[1:]
            if gh.startswith("^"):
                gh = gh[1:]
            if gh.startswith("$"):
                gmods = _sanitize_mods(gh[1:])
                if gmods and check_modifiers(gmods, is_allow):
                    return [("domain", ("@@" if is_allow else "") + "*$" + normalize_mods(gmods))]

        # |domain| / |domain^：两端/主机结尾锚定为单主机名；不要扩大成整个子域。
        if body.startswith("|") and not body.startswith("||"):
            d = body[1:]
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            exact_end = d.endswith("|")
            exact_end_caret = d.endswith("^")
            if exact_end or exact_end_caret:
                d = d[:-1].rstrip(".")
                if ("/" not in d and ":" not in d and DOMAIN_CHARS.fullmatch(d) and "." in d
                        and not is_ip(d) and is_valid_domain(d)
                        and (not dmods or check_modifiers(dmods, is_allow))):
                    suffix = "|" if exact_end else "^"
                    rule = ("@@" if is_allow else "") + f"|{d.lower()}{suffix}"
                    if dmods:
                        rule += "$" + normalize_mods(dmods)
                    return [("domain", rule)]
            # 单起始锚点 + ^ 仍然与 ||domain^ 同义，可安全收窄为 AGH 域名边界规则。
            if d.endswith("^"):
                d = d[:-1].rstrip(".")
                if ("/" not in d and ":" not in d and DOMAIN_CHARS.fullmatch(d) and "." in d
                        and not is_ip(d) and is_valid_domain(d)
                        and (not dmods or check_modifiers(dmods, is_allow))):
                    rule = ("@@" if is_allow else "") + f"||{d.lower()}^"
                    if dmods:
                        rule += "$" + normalize_mods(dmods)
                    return [("domain", rule)]

        # domain^：无显式 || 的传统规则，按本构建器既有策略归一化。
        if not body.startswith("|"):
            d = body
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            if d.endswith("^"):
                d = d[:-1].rstrip(".")
                if ("/" not in d and ":" not in d and DOMAIN_CHARS.fullmatch(d) and "." in d
                        and not is_ip(d) and is_valid_domain(d)
                        and (not dmods or check_modifiers(dmods, is_allow))):
                    rule = ("@@" if is_allow else "") + f"||{d.lower()}^"
                    if dmods:
                        rule += "$" + normalize_mods(dmods)
                    return [("domain", rule)]

        # hosts 行：允许多个 alias，并忽略 # 后注释。
        m = HOSTS_IP_PREFIX.match(line)
        if m:
            rest = line[m.end():]
            if "#" in rest:
                rest = rest.split("#", 1)[0]
            out = []
            for tok in rest.split():
                d = tok.lower().rstrip(".")
                if not d or is_ip(d) or not is_valid_hosts_hostname(d):
                    continue
                # hosts 语法官方语义只匹配该主机名本身、不匹配子域，
                # 归一化为精确单主机 |host|（||host^ 会扩大拦截到子域，改变语义）。
                out.append(("domain", ("@@" if is_allow else "") + f"|{d}|"))
            return out

        # domains-only 纯域名：官方语义只匹配该主机名本身、不匹配子域，
        # 归一化为精确单主机 |host|（||host^ 会扩大到子域，改变语义）。
        if ("/" not in line and ":" not in line and "#" not in line
                and DOMAIN_CHARS.fullmatch(line) and "." in line):
            d = line.rstrip(".")
            if not is_ip(d) and is_valid_domain(d):
                return [("domain", ("@@" if is_allow else "") + f"|{d.lower()}|")]

        # domain|：结尾锚点无起始边界，example.com| 会误配 notexample.com / b.notexample.com。
        # 归一化为 ||domain^（有域名边界）：| 结尾本就匹配域名本身及全部子域，
        # 归一化匹配范围等价，同时消除无边界后缀误配，是严格更安全的等价替换。
        if not body.startswith("|"):
            d2 = body
            d2mods = ""
            if "$" in d2:
                d2, d2mods = d2.split("$", 1)
            if d2.endswith("|") and "/" not in d2 and ":" not in d2 and "#" not in d2:
                d2 = d2[:-1].rstrip(".")
                if (DOMAIN_CHARS.fullmatch(d2) and "." in d2 and not is_ip(d2)
                        and is_valid_domain(d2)
                        and (not d2mods or check_modifiers(d2mods, is_allow))):
                    rule = ("@@" if is_allow else "") + f"||{d2.lower()}^"
                    if d2mods:
                        rule += "$" + normalize_mods(d2mods)
                    return [("domain", rule)]

        # @@domain：裸域名白名单官方语义只放行该主机名本身 -> @@|domain|；
        # @@domain^：结尾指针无开头边界，按 v2.5 策略加边界归一化为 @@||domain^（少放行，安全）。
        if is_allow:
            d = body
            dmods = ""
            if "$" in d:
                d, dmods = d.split("$", 1)
            has_caret = d.endswith("^")
            if has_caret:
                d = d[:-1]
            d = d.rstrip(".")
            if ("/" not in d and ":" not in d and DOMAIN_CHARS.fullmatch(d) and "." in d
                    and not is_ip(d) and is_valid_domain(d)
                    and (not dmods or check_modifiers(dmods, is_allow))):
                rule = f"@@||{d.lower()}^" if has_caret else f"@@|{d.lower()}|"
                if dmods:
                    rule += "$" + normalize_mods(dmods)
                return [("domain", rule)]
        return []

    # ||domain 规则。
    rest = body[2:]
    mods = ""
    if "$" in rest:
        rest, mods = rest.split("$", 1)
    if "/" in rest or ":" in rest:
        return []
    domain = rest[:-1] if rest.endswith("^") else rest
    domain = domain.lower().rstrip(".")
    if not domain or not DOMAIN_CHARS.fullmatch(domain) or is_ip(domain) or not is_valid_domain(domain):
        return []
    if mods and not check_modifiers(mods, is_allow):
        return []
    rule = ("@@" if is_allow else "") + f"||{domain}^"
    if mods:
        rule += "$" + normalize_mods(mods)
    return [("domain", rule)]


# 条件修饰符：带它们的规则只在「特定客户端 / 记录类型 / 客户端标签 / 排除域」下生效，
# 不是无条件匹配整个域名。构建期做域名级冲突剔除时不处理这类规则，交给 AGH 运行时判定。
CONDITIONAL_MODIFIERS = {"client", "dnstype", "ctag", "denyallow"}


def _build_domain_matchers(rules):
    """构建普通/important 例外的域名匹配器；额外支持 @@|host| 精确单主机例外。

    @@|host|（结尾指针）才是精确单主机放行；@@|host^ 是前缀锚定（还匹配 host.xxx），
    不能当作精确单主机，保守不收入（少删，安全）。
    """
    exact, wild, pat, host_exact = set(), set(), [], set()
    for r in rules:
        mods = _rule_modifier_names(r)
        if mods & CONDITIONAL_MODIFIERS:
            continue
        if r.startswith("@@||"):
            d = extract_domain(r)
            if not d:
                continue
            if d.startswith("*."):
                wild.add(d[2:])
            elif "*" in d:
                pat.append(re.compile(fnmatch.translate(d)))
            else:
                exact.add(d)
            continue
        # 仅 @@|host| 精确单主机；@@|host^ 不收入（前缀锚定语义更大，建模为单主机不安全）。
        if r.startswith("@@|"):
            head, _ = _parse_rule_modifiers(r)
            body_h = head[3:] if head.startswith("@@|") else ""
            if body_h.endswith("|"):
                d = body_h[:-1].rstrip(".").lower()
                if d and "*" not in d:
                    host_exact.add(d)
    return exact, wild, pat, host_exact


def _domain_matches(struct, dom: str, strict: bool = True, allow_host_exact: bool = True) -> bool:
    exact, wild, pat, host_exact = struct
    # host_exact（@@|host| 单主机放行）只在调用方确认拦截规则也是精确单主机时才允许使用：
    # 域拦截规则 ||dom^ 还覆盖子域，单主机白名单不足以放行整条，不能据此删除。
    if allow_host_exact and dom in host_exact:
        return True
    if not strict:
        return dom in exact
    parts = dom.split(".")
    for i in range(len(parts)):
        suffix = ".".join(parts[i:])
        if suffix in exact:
            return True
        if suffix in wild and i > 0:
            return True
    return any(p.fullmatch(dom) for p in pat)


def _build_rewrite_matcher(rules):
    """构建 dnsrewrite 例外匹配器：域名结构 + rewrite 值双重匹配。"""
    exact, wild, pat, host_exact = {}, {}, [], {}
    for r in rules:
        if "dnsrewrite" not in _rule_modifier_names(r):
            continue
        mods = _rule_modifier_names(r)
        if mods & CONDITIONAL_MODIFIERS:
            continue
        d = extract_domain(r)
        if not d:
            continue
        vals = _modifier_values(r, "dnsrewrite")
        if not vals:
            continue
        negated, sep, value = vals[0]
        if negated:
            continue
        rewrite_value = value if sep else None
        if r.startswith("@@||"):
            if d.startswith("*."):
                wild.setdefault(d[2:], set()).add(rewrite_value)
            elif "*" in d:
                pat.append((re.compile(fnmatch.translate(d)), {rewrite_value}))
            else:
                exact.setdefault(d, set()).add(rewrite_value)
        elif r.startswith("@@|"):
            head, _ = _parse_rule_modifiers(r)
            body_h = head[3:] if head.startswith("@@|") else ""
            if body_h.endswith("|"):
                host = body_h[:-1].rstrip(".").lower()
                if host:
                    host_exact.setdefault(host, set()).add(rewrite_value)
    return exact, wild, pat, host_exact


def _rewrite_matches(matcher, dom: str, value: str, strict: bool = True,
                     allow_host_exact: bool = True) -> bool:
    exact, wild, pat, host_exact = matcher

    def has_value(values):
        return None in values or value in values

    if allow_host_exact and dom in host_exact and has_value(host_exact[dom]):
        return True
    if not strict:
        return dom in exact and has_value(exact[dom])
    parts = dom.split(".")
    for i in range(len(parts)):
        suffix = ".".join(parts[i:])
        values = exact.get(suffix)
        if values is not None and has_value(values):
            return True
        if i > 0:
            values = wild.get(suffix)
            if values is not None and has_value(values):
                return True
    return any(p.fullmatch(dom) and has_value(values) for p, values in pat)


def filter_conflicts(blacklist: list, allowlist: list, strict: bool = True) -> list:
    """按 AGH 规则优先级做安全的冲突剔除，保证零误删。

    - 正则：仅当 ``_regex_safe_to_remove``（结尾 $ 锚定 + 可靠解析 + 保证后缀全覆盖）才删除。
    - |host| 精确单主机拦截：被 @@||host^（域放行）或 @@|host|（单主机放行）覆盖时删除。
    - ||host^ 域拦截：必须被域白名单（@@||…^，严格模式含子域）覆盖才删除；
      @@|host| 只放行单主机，不删除域规则（其仍覆盖子域）。
    - |host^ 前缀锚定拦截：还匹配 host.xxx，保守保留。
    """
    ordinary = _build_domain_matchers(
        [r for r in allowlist if "important" not in _rule_modifier_names(r)
         and "dnsrewrite" not in _rule_modifier_names(r)]
    )
    powerful = _build_domain_matchers(
        [r for r in allowlist if "important" in _rule_modifier_names(r)
         and "dnsrewrite" not in _rule_modifier_names(r)]
    )
    rewrite = _build_rewrite_matcher(allowlist)

    kept, removed, imp_kept, removed_regex = [], 0, 0, 0
    for rule in blacklist:
        if rule.startswith("/"):
            rmods = _rule_modifier_names(rule)
            if rmods & CONDITIONAL_MODIFIERS or "dnsrewrite" in rmods:
                kept.append(rule)          # 条件 / dnsrewrite 正则：运行时判定
                continue
            if _regex_safe_to_remove(rule, ordinary, powerful, strict):
                removed += 1
                removed_regex += 1
            else:
                kept.append(rule)
            continue

        mods = _rule_modifier_names(rule)
        body_r = rule[2:] if rule.startswith("@@") else rule
        is_exact_single = False
        if body_r.startswith("|") and not body_r.startswith("||"):
            head0, _ = _parse_rule_modifiers(rule)
            pat0 = head0[2:] if head0.startswith("@@") else head0[1:]
            is_exact_single = pat0.endswith("|")
            if not is_exact_single:
                # |host^ 前缀锚定：可能匹配 host.evil.com，保守保留
                kept.append(rule)
                continue

        d = extract_domain(rule)
        if not d:
            kept.append(rule)
            continue

        is_imp = "important" in mods
        if "dnsrewrite" in mods:
            vals = _modifier_values(rule, "dnsrewrite")
            if not vals or vals[0][0] or not vals[0][1]:
                kept.append(rule)
                continue
            conflict = _rewrite_matches(rewrite, d, vals[0][2], strict=strict,
                                       allow_host_exact=is_exact_single)
        elif is_imp:
            conflict = _domain_matches(powerful, d, strict=strict,
                                      allow_host_exact=is_exact_single)
            if not conflict:
                imp_kept += 1
        else:
            conflict = (_domain_matches(ordinary, d, strict=strict,
                                       allow_host_exact=is_exact_single) or
                        _domain_matches(powerful, d, strict=strict,
                                       allow_host_exact=is_exact_single))
        if conflict:
            removed += 1
        else:
            kept.append(rule)

    n_alw = len(allowlist)
    log(f"白名单冲突剔除: {removed} 条黑名单规则（正则 {removed_regex} 条经可靠判定全部匹配名被白名单覆盖，"
        f"{'严格' if strict else '精确'}匹配，白名单规则 {n_alw} 条，保留 important 拦截 {imp_kept} 条）")
    return kept


def apply_badfilter(rules: list):
    """
    在构建阶段解析 $badfilter。支持普通 domain 和 regex 规则，并正确处理 regex 内部的 $。
    返回 (保留规则 list, 被禁用数, 悬空指令数)。
    """
    target_keys = set()
    normal = []
    for r in rules:
        _, mods = _parse_rule_modifiers(r)
        if mods is None:
            normal.append(r)
            continue
        names = {m.lstrip("~").split("=", 1)[0].strip().lower() for m in mods}
        if "badfilter" not in names:
            normal.append(r)
            continue
        head, _ = _parse_rule_modifiers(r)
        kept_mods = [m for m in mods if m.lstrip("~").split("=", 1)[0].strip().lower() != "badfilter"]
        target = head if not kept_mods else head + "$" + normalize_mods(",".join(kept_mods))
        target_keys.add(_rule_key(target))

    key_map = {_rule_key(r): r for r in normal}
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
    父域覆盖去重；同时处理域规则（||）与精确单主机规则（|host|），拦截/白名单两组独立。

    组内结构：plain {dom: "d"(域规则) / "s"(单主机)}，leftwild（*.base 左通配）。
    覆盖关系：
      ||example.com^ 覆盖 ||www.example.com^ 与 |www.example.com|；
      ||*.example.com^ 覆盖所有子域形式；
      域规则与单主机规则指向同一主机时，域规则更强（覆盖子域），保留域规则。
    一律保留：带修饰符、中间通配、|host^ 前缀锚定、正则、单段规则。
    strict=False（--no-strict）时白名单不做去重（父域不放行子域）；黑名单去重不受影响。
    返回 (精简后规则 list, 移除数)。
    """
    groups, other = {}, []
    merged_away = 0
    for r in rules:
        is_alw = r.startswith("@@")
        gk = "alw" if is_alw else "blk"
        body = r[2:] if is_alw else r
        if not body.startswith("|") or "$" in r:
            other.append(r)
            continue
        grp = groups.setdefault(gk, [{}, set()])
        if body.startswith("||"):
            b = body[2:]
            if "/" in b or ":" in b:
                other.append(r); continue
            if b.endswith("^"):
                b = b[:-1]
            b = b.rstrip(".").lower()
            if "*" in b:
                if b.startswith("*."):
                    if is_alw and not strict:
                        other.append(r); continue
                    grp[1].add(b[2:])
                    continue
                other.append(r); continue
            if "." in b:
                if is_alw and not strict:
                    other.append(r); continue
                if grp[0].get(b) == "s":
                    merged_away += 1      # 同主机单主机规则被域规则替代，计一条精简
                grp[0][b] = "d"          # 域规则强于单主机，直接覆盖标记
                continue
            other.append(r); continue
        # 单 | 锚定：仅 |host| 精确单主机参与；|host^ 前缀锚定保留。
        b = body[1:]
        if b.endswith("|"):
            host = b[:-1].rstrip(".").lower()
            if "." in host and "/" not in host and ":" not in host:
                if is_alw and not strict:
                    other.append(r); continue
                if grp[0].get(host) != "d":
                    grp[0][host] = "s"
                continue
        other.append(r)

    kept = list(other)
    removed = merged_away
    for gk, (plain, leftwild) in groups.items():
        is_alw = gk == "alw"
        dom_pre = "@@||" if is_alw else "||"
        single_pre = "@@|" if is_alw else "|"

        def covered(dom: str) -> bool:
            parts = dom.split(".")
            for i in range(1, len(parts)):    # i>=1：只比对严格父后缀
                suf = ".".join(parts[i:])
                if suf in plain or suf in leftwild:
                    return True
            return False

        for base in leftwild:                 # 左通配：同主机域规则(d)存在或父域被覆盖即冗余；
            if plain.get(base) == "d" or covered(base):  # 同主机仅单主机(s)时两者范围不同，均保留
                removed += 1
            else:
                kept.append(dom_pre + "*." + base + "^")
        for dom, kind in plain.items():   # 裸域/单主机：被父域/左通配覆盖即冗余
            if covered(dom):
                removed += 1
            elif kind == "s":
                kept.append(single_pre + dom + "|")
            else:
                kept.append(dom_pre + dom + "^")
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
    构建中断/进程被杀不会留下半截文件，dist 产物始终保持完整可读。
    写失败时清理残留的 .tmp 文件，避免下次构建读到陈旧中间态。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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
        # hosts 没有 modifiers，也没有子域继承语义；只允许真正的“纯域名无 modifier”规则。
        # 因此不能把 $important / $client / $dnstype 等语义静默转换成全局 hosts。
        hosts_blk = sorted(set(hosts_source if hosts_source is not None else blacklist), key=sort_key)
        hosts_domains = set()
        for r in hosts_blk:
            if "$" in r or "*" in r or "?" in r:
                continue
            if r.startswith("||") and r.endswith("^"):
                d = r[2:-1].rstrip(".").lower()
            elif r.startswith("@@"):
                continue
            elif r.startswith("|") and (r.endswith("|") or r.endswith("^")):
                d = r[1:-1].rstrip(".").lower()
            else:
                # domain| is an unbounded suffix/end-anchor pattern and cannot be safely
                # represented by one hosts entry without changing its matching range.
                continue
            if is_valid_hosts_hostname(d):
                hosts_domains.add(d)
        domains = sorted(hosts_domains)
        atomic_write(CONFIG["out_hosts"],
                     "# Title: " + CONFIG["title"] + "（Hosts）\n"
                     + f"# Total count: {len(domains)}\n"
                     + "\n".join(f"0.0.0.0 {d}" for d in domains) + "\n")
        log(f"✅ {CONFIG['out_hosts'].name}: {len(domains)} 条")

    else:
        # 避免 --no-hosts 时旧 hosts.txt 残留，导致发布目录看起来仍有过期 hosts。
        try:
            if CONFIG["out_hosts"].exists():
                CONFIG["out_hosts"].unlink()
                log(f"🗑 已删除旧的 {CONFIG['out_hosts'].name}")
        except OSError as e:
            log(f"⚠ 无法删除旧的 {CONFIG['out_hosts'].name}: {e}")

    # 4) 统计 JSON
    def report_path(path: Path):
        try:
            return str(path.relative_to(BASE_DIR))
        except ValueError:
            return str(path)

    stats["output"] = {
        "rules_file": report_path(CONFIG["out_rules"]),
        "rules_count": len(all_blk),
        "allow_file": report_path(CONFIG["out_allow"]),
        "allow_count": len(alw),
        "hosts_file": report_path(CONFIG["out_hosts"]) if CONFIG["keep_hosts"] else None,
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
        # 源清单中的 URL 允许 # fragment；仅把空白后的 # 视为注释。
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
    if lines is FETCH_PERMANENT_FAILURE:
        ok = False
        retryable = False
        lines = []
    elif lines is None:
        ok = False
        retryable = True
        lines = []
    else:
        ok = True
        retryable = False
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
    return blk, alw, reg, ok, retryable


def download_all(merged: list, stats: dict):
    """并发下载+清洗；仅网络临时失败进入第二轮，确定性失败不重复尝试。"""
    def run_round(sources):
        results, retryable_failed, permanent_failed = {}, [], []
        with cf.ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as ex:
            futures = {ex.submit(process_source, s, stats, is_allow): (s, is_allow)
                       for s, is_allow in sources}
            for fut in cf.as_completed(futures):
                s, is_allow = futures[fut]
                try:
                    blk, alw, reg, ok, retryable = fut.result()
                except Exception as e:
                    log(f"✗ 处理源异常: {s}  ({str(e)[:100]})")
                    if s.startswith(("http://", "https://")):
                        retryable_failed.append((s, is_allow))
                    else:
                        permanent_failed.append((s, is_allow))
                    continue
                if ok:
                    results[s] = (blk, alw, reg)
                elif retryable:
                    retryable_failed.append((s, is_allow))
                else:
                    permanent_failed.append((s, is_allow))
        return results, retryable_failed, permanent_failed

    results, retryable, permanent = run_round(merged)
    if retryable:
        log(f"⚠ 第一轮 {len(retryable)} 个网络源失败，等待 {CONFIG['retry_wait']}s 后第二轮补拉…")
        time.sleep(CONFIG["retry_wait"])
        results2, retryable, permanent2 = run_round(retryable)
        results.update(results2)
        permanent.extend(permanent2)
        if results2:
            log(f"✓ 第二轮补拉成功 {len(results2)} 个源")
    failed = permanent + retryable
    if failed:
        log(f"⚠ 最终仍有 {len(failed)} 个源失败（不会用失败源的旧数据冒充成功）")
    return results, failed

def main():
    ap = argparse.ArgumentParser(description="AGH-Builder: 只适配 AdGuard Home 的规则合并器")
    ap.add_argument("--no-regex", action="store_true", help="不保留正则规则")
    ap.add_argument("--no-hosts", action="store_true", help="不生成 hosts 文件")
    ap.add_argument("--no-strict", action="store_true", help="白名单冲突仅精确匹配（默认含子域名）")
    ap.add_argument("--sources", default=None, help="自定义黑名单源文件")
    ap.add_argument("--allow-sources", default=None, help="自定义白名单源文件")
    args = ap.parse_args()

    # main() 可能在测试/嵌入场景中被重复调用；每次以 CLI 参数为准，避免上一次调用的 flag 泄漏。
    CONFIG["keep_regex"] = not args.no_regex
    CONFIG["keep_hosts"] = not args.no_hosts
    CONFIG["strict_conflict"] = not args.no_strict

    black_sources = load_sources(resolve_path(args.sources) if args.sources else CONFIG["sources_file"])
    allow_sources = load_sources(resolve_path(args.allow_sources) if args.allow_sources else CONFIG["allow_sources_file"])
    log(f"拦截源 {len(black_sources)} 个，白名单源 {len(allow_sources)} 个，开始并发下载…")

    stats = {
        "builder": "AGH-Builder",
        "version": "2.6",
        "generated_at": datetime.datetime.now(
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

    # 并发下载+清洗全部源（网络失败源等待退避窗口后自动补拉一轮）
    results, failed = download_all(merged, stats)

    # 安全保护：如果一个角色下的所有“有效源”都失败，不生成该角色的空列表，
    # 防止网络故障把已有黑名单/白名单整批冲掉。重复出现在黑名单与白名单的源按黑名单角色处理。
    effective_black = [s for s, is_allow in merged if not is_allow]
    effective_allow = [s for s, is_allow in merged if is_allow]
    successful_black = [s for s in effective_black if s in results]
    successful_allow = [s for s in effective_allow if s in results]
    if effective_black and not successful_black:
        log("✗ 所有拦截源均失败，中止构建（保留 dist/ 现有产物）")
        return 1
    if effective_allow and not successful_allow:
        log("✗ 所有白名单源均失败，中止构建（保留 dist/ 现有产物）")
        return 1

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

    # 解析 $badfilter：黑名单域名 + 黑名单正则统一处理；白名单独立处理。
    blk_kept, blk_bf, blk_dangle = apply_badfilter(list(all_blk) + list(all_reg))
    all_blk = set(r for r in blk_kept if not r.startswith("/"))
    all_reg = set(r for r in blk_kept if r.startswith("/"))
    alw_kept, alw_bf, alw_dangle = apply_badfilter(list(all_alw))
    all_alw = set(alw_kept)
    if blk_bf or alw_bf:
        log(f"badfilter 解析: 禁用黑名单 {blk_bf} 条、白名单 {alw_bf} 条（悬空指令 "
            f"{blk_dangle + alw_dangle} 条，无对应规则，已丢弃）")

    # 白名单冲突处理（--no-strict 时退化为精确匹配，仍生效）。
    # 正则仅在全部候选域名都被白名单覆盖时才删除（见 filter_conflicts）。
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

    # stats.json 也按源清单顺序稳定输出，避免并发完成顺序导致无意义 diff。
    stats["sources"] = {
        s: stats["sources"][s] for s, _ in merged if s in stats["sources"]
    }
    write_outputs(all_blk, all_alw, all_reg, stats, hosts_source=blk_for_hosts)
    log("=" * 56)
    log(f"完成！最终规则: 拦截 {len(all_blk) + len(all_reg)} 条 + 白名单 {len(all_alw)} 条")
    log(f"输出目录: {DIST_DIR}")


if __name__ == "__main__":
    sys.exit(main() or 0)
