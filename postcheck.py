#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# AGH-Builder 双向自检 v2.2：
#  1) 功能域被拦 -> 误伤 -> 加白 wan.txt + 重建（return 1）
#  2) 广告域未拦 -> 漏拦 -> 追加 hardblock.txt（HARD_BLOCK_WL 外部源）+ 重建（return 2）
#  v2.1: check() 返回 ERR（AGH 查询异常/临时故障）时跳过该域，
#        防止 AGH 重启/过载窗口把全部广告域误判为漏拦并污染 hardblock.txt。
#  v2.2: 域名检查改为并发（默认 10 线程），check 已能识别 CNAME 链拦截，
#        自检窗口从数分钟缩短到数十秒；ERR 占比过半时按整体异常处理，不误判。
#  v2.3: 整体异常（检测能力故障）返回 rc=3，与"规则误伤/漏拦(rc=1/2)"区分，
#        wrapper 对 rc=3 不触发无意义重建，只记录告警，杜绝静默"假通过"。
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "/opt/agh-builder"
WLIST = os.path.join(BASE, "postcheck_domains.txt")       # 功能域
ALIST = os.path.join(BASE, "adcheck_domains.txt")         # 广告域
WAN = os.path.join(BASE, "local", "wan.txt")
HARD = os.path.join(BASE, "local", "hardblock.txt")
LOG = "/var/log/agh-postcheck.log"
NL = chr(10)
WORKERS = 10
ERR_HALF = 0.5


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + NL)


def check(h):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8091/api/check?host=" + h, timeout=10) as r:
            return json.loads(r.read().decode()).get("reason", "")
    except Exception:
        return "ERR"


def check_all(hosts):
    """并发查询，返回 (results, errs, degraded)。ERR 占比过半时视为整体异常，
    results 置空、degraded=True（本轮无法判定，不应静默记为通过）。"""
    results = {}
    errs = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for h, r in ex.map(lambda x: (x, check(x)), hosts):
            if r == "ERR":
                errs += 1
            else:
                results[h] = r
    if hosts and errs >= len(hosts) * ERR_HALF:
        return {}, errs, True
    return results, errs, False


def load_list(p):
    # 支持每行多个域名（空白分隔）
    with open(p, encoding="utf-8") as f:
        out = []
        for l in f:
            l = l.split("#", 1)[0].strip()  # 同时剥离整行与行内注释
            if not l:
                continue
            out.extend(l.split())
        return out


def append_unique(path, items):
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l and not l.startswith("#"):
                    existing.add(l.split()[0])
    added = []
    with open(path, "a", encoding="utf-8") as f:
        for it in items:
            if it not in existing:
                f.write(it + NL)
                added.append(it)
    return added


def load_permanent_blocks():
    """永久拦截清单：local/hardblock.txt（手动永久拦截）+ 构建审计 hardblock.log
    （含内置强制拦截与 $important 推导）。命中其中的功能域属于永久拦截意图，
    自检不再尝试加白，避免“加白->被永久拦截层剔除->复检仍拦->再加白”的死循环重建。"""
    exact, wildcard = set(), []

    def add(dom):
        dom = dom.lower().rstrip(".")
        if "*" in dom:
            wildcard.append(re.compile(
                "^" + "".join(".*" if c == "*" else re.escape(c) for c in dom) + "$"))
        elif dom:
            exact.add(dom)

    if os.path.exists(HARD):
        with open(HARD, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    add(line.split()[0])
    audit = os.path.join(BASE, "dist", "audit", "hardblock.log")
    if os.path.exists(audit):
        with open(audit, encoding="utf-8") as f:
            for line in f:
                m = re.search(r"@@\|\|([a-z0-9._\-*]+)\^", line)
                if m:
                    add(m.group(1))
    return exact, wildcard


def is_permanent(h, exact, wildcard):
    """域自身或任一父域命中永久拦截精确清单，或命中通配规则。"""
    labels = h.lower().rstrip(".").split(".")
    for i in range(len(labels)):
        d = ".".join(labels[i:])
        if d in exact:
            return True
        for cre in wildcard:
            if cre.match(d):
                return True
    return False


def main():
    rc = 0
    # 1) 误伤检测：功能域被拦（ERR 跳过，防 AGH 故障窗口误判）
    try:
        whosts = load_list(WLIST)
        results, errs, degraded = check_all(whosts)
        perm_exact, perm_wild = load_permanent_blocks()
        blocked, perm_skipped = [], []
        for h, r in results.items():
            if r != "FilteredBlackList":
                continue
            if is_permanent(h, perm_exact, perm_wild):
                perm_skipped.append(h)
            else:
                blocked.append(h)
        if errs:
            log("误伤检测跳过 %d 个（AGH 查询异常）" % errs)
        if degraded:
            log("误伤检测整体异常（ERR 过半），本轮无法判定")
            if rc == 0:
                rc = 3
        if perm_skipped:
            log("永久拦截域跳过加白 %d 个（命中 hardblock 永久清单，不触发修复）: %s"
                % (len(perm_skipped), ",".join(perm_skipped)))
        if blocked:
            added = append_unique(WAN, ["@@||%s^" % h for h in blocked])
            log("误伤 %d 个: %s -> wan.txt 加白 %s" % (len(blocked), ",".join(blocked), ",".join(added)))
            rc = 1
    except Exception as e:
        log("误伤检测异常: %s" % e)
    # 2) 漏拦检测：广告域未被拦（ERR 跳过）
    try:
        ahosts = load_list(ALIST)
        results, errs, degraded = check_all(ahosts)
        leaked = [h for h, r in results.items() if r != "FilteredBlackList"]
        if errs:
            log("漏拦检测跳过 %d 个（AGH 查询异常）" % errs)
        if degraded:
            log("漏拦检测整体异常（ERR 过半），本轮无法判定")
            if rc == 0:
                rc = 3
        if leaked:
            added = append_unique(HARD, leaked)
            log("漏拦 %d 个: %s -> hardblock.txt 追加 %s" % (len(leaked), ",".join(leaked), ",".join(added)))
            rc = 2
    except Exception as e:
        log("漏拦检测异常: %s" % e)
    if rc == 0:
        log("自检通过: 功能域 %d 个 + 广告域 %d 个" % (len(load_list(WLIST)), len(load_list(ALIST))))
    return rc


if __name__ == "__main__":
    sys.exit(main())
