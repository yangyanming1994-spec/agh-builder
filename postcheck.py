#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# AGH-Builder 双向自检 v2.1:
#  1) 功能域被拦 -> 误伤 -> 加白 wan.txt + 重建（return 1）
#  2) 广告域未拦 -> 漏拦 -> 追加 hardblock.txt（HARD_BLOCK_WL 外部源）+ 重建（return 2）
#  v2.1: check() 返回 ERR（AGH 查询异常/临时故障）时跳过该域，
#        防止 AGH 重启/过载窗口把全部广告域误判为漏拦并污染 hardblock.txt。
import json
import os
import sys
import time
import urllib.request

BASE = "/opt/agh-builder"
WLIST = os.path.join(BASE, "postcheck_domains.txt")       # 功能域
ALIST = os.path.join(BASE, "adcheck_domains.txt")         # 广告域
WAN = os.path.join(BASE, "local", "wan.txt")
HARD = os.path.join(BASE, "local", "hardblock.txt")
LOG = "/var/log/agh-postcheck.log"
NL = chr(10)


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + NL)


def check(h):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8091/api/check?host=" + h, timeout=5) as r:
            return json.loads(r.read().decode()).get("reason", "")
    except Exception:
        return "ERR"


def load_list(p):
    # 支持每行多个域名（空白分隔）
    with open(p, encoding="utf-8") as f:
        out = []
        for l in f:
            l = l.strip()
            if not l or l.startswith("#"):
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


def main():
    rc = 0
    # 1) 误伤检测：功能域被拦（ERR 跳过，防 AGH 故障窗口误判）
    try:
        whosts = load_list(WLIST)
        results = {}
        errs = 0
        for h in whosts:
            r = check(h)
            if r == "ERR":
                errs += 1
                continue
            results[h] = r
        blocked = [h for h, r in results.items() if r == "FilteredBlackList"]
        if errs:
            log("误伤检测跳过 %d 个（AGH 查询异常）" % errs)
        if blocked:
            added = append_unique(WAN, ["@@||%s^" % h for h in blocked])
            log("误伤 %d 个: %s -> wan.txt 加白 %s" % (len(blocked), ",".join(blocked), ",".join(added)))
            rc = 1
    except Exception as e:
        log("误伤检测异常: %s" % e)
    # 2) 漏拦检测：广告域未被拦（ERR 跳过）
    try:
        ahosts = load_list(ALIST)
        results = {}
        errs = 0
        for h in ahosts:
            r = check(h)
            if r == "ERR":
                errs += 1
                continue
            results[h] = r
        leaked = [h for h, r in results.items() if r != "FilteredBlackList"]
        if errs:
            log("漏拦检测跳过 %d 个（AGH 查询异常）" % errs)
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
