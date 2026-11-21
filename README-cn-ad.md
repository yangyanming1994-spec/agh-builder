# 国内广告联盟 · 自动补充 / 加固 / 防退化

## 一句话

国内 60+ 个主流广告联盟域 + 一条 `ad/ads` 段宽正则，全部固化在自有清单里，上游源改了删了也不影响；每小时自检，掉了自动重建恢复；有页面看状态。

## 三层防线

| 层 | 内容 | 位置 | 失效时 |
|---|---|---|---|
| 1 显式基线 | 56 条具体域名（字节/腾讯/阿里/快手/OPPO/小米/华为/荣耀/红果/小红书/高德等） | `local/cn-ad-networks.txt` [A]~[D] 段 | 审计报 fail 并自动重建 |
| 2 本地宽正则 | `/(^|[._/-])ads?([._/-]|$)/`，覆盖 150 个域 | 同文件 [E] 段 | 探针 `probe-ads.zznx-probe-zz` 变非 0.0.0.0 即报 |
| 3 上游源 | 10 个第三方清单（oisd / adrules / anti-AD / yhosts / 666 / hagezi 等） | `sources.txt` | 只做增量，不再依赖 |

## 组件

| 组件 | 路径 | 频率 |
|---|---|---|
| 审计 | `/usr/local/bin/cn-ad-net-audit.py` | 每小时 :17 |
| 发现 | `/usr/local/bin/cn-ad-net-discover.py` | 周日 03:45 |
| 构建 | `/usr/local/bin/build-and-refresh.sh` | 按需 / 自愈触发 |
| 页面 | `https://dns.sofmed.cn/cn-ad` | 实时 |
| 接口 | `/api/cn-ad-health`、`/api/cn-ad-candidates` | 实时 |
| 快照 | `state/cn-ad-health.json` | 每次审计覆写 |
| 趋势 | `state/cn-ad-history.jsonl` | 按天折叠 |

## 审计做了什么

1. 56 条基线并发 dig（`@::1`），非 0.0.0.0 即失效
2. 12 个业务域抽检，被拦即误杀
3. 宽正则探针，确认主力规则存活
4. 覆盖来源分类：显式 / 父域 / 通配 / 仅上游正则 / 无
5. 规则总量对比，较上次跌超 10% 报警
6. 有失效 → 触发重建 → 复检 → 恢复则继续

## 自愈

- 触发条件：任一条基线失效
- 动作：`build-and-refresh.sh`（约 160s，含 17 项回归）
- 冷却：1 小时（`state/cn-ad-heal.last`）
- 复检：只重测失效域
- 实测：2026-11-21 18:00，注入 NXDOMAIN 域 → 163s 重建 → 1→0 恢复

## 状态语义

- `ok`：基线全绿 + 无误杀 + 探针活 + 无"零覆盖"条目
- `degraded`：上次 ok 本次掉（基线失效或总量骤降）
- `alert`：持续异常（误杀 / 探针死 / 零覆盖）

## 常用命令

```bash
# 当前状态
python3 /usr/local/bin/cn-ad-net-audit.py

# 手动重建（改完规则必跑）
bash /usr/local/bin/build-and-refresh.sh

# 验证某域
dig @::1 <domain> +short     # 0.0.0.0 = 已拦

# 查命中哪条规则
curl -u "$AGH_USER:$AGH_PASS" \
  "http://127.0.0.1:3000/control/filtering/check_host?name=<domain>"

# 全量发现
python3 /usr/local/bin/cn-ad-net-discover.py --limit 0

# 推 GitHub
python3 /usr/local/bin/gh_push.py local/cn-ad-networks.txt
```

## 加新平台

1. 在 `local/cn-ad-networks.txt` 对应分组加 `||domain^`
2. 跑 `build-and-refresh.sh`
3. 跑审计确认 56→N 全绿
4. 推 GitHub

## 不要拦的

写进 `[B]` 段留作记录（`mbd.baidu.com`、`umeng.com`、`nc.music.163.com` 等），或在 `local/whitelist.txt` 加 `@@||domain^`。

已知误伤已放行：`anti-ad.net`（anti-AD 项目官网，规则提供方）。

## 回滚

```bash
# 清单回滚
cp local/cn-ad-networks.txt.bak-<ts> local/cn-ad-networks.txt
bash /usr/local/bin/build-and-refresh.sh

# 审计回滚
cp /usr/local/bin/cn-ad-net-audit.py.bak-v5 /usr/local/bin/cn-ad-net-audit.py

# 页面回滚
cp /usr/local/bin/status-server-py3.py.bak-cnad5 /usr/local/bin/status-server-py3.py
systemctl restart status-server
```
