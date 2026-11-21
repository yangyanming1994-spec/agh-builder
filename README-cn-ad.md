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

## v8 审计修复（2026-11-21）

审计脚本 v8 修掉 4 个会导致「假绿 / 假自愈 / 漏报」的缺陷：

| # | 缺陷 | 后果 | v8 修法 |
|---|------|------|---------|
| 1 | `dig` 超时只 `communicate(timeout=8)` 不 kill 子进程 | 每小时可能残留 dig，长期累积 | `TimeoutExpired` 后 `p.kill()` + 回收 |
| 2 | 自愈看 `build-and-refresh.sh` 的 rc=0 就算成功，但该脚本遇构建锁只打印「已有构建进行中，跳过」并 exit 0 | 并发构建时自愈**永远假成功**，冷却戳已写死 → 真故障一小时内不再重试 | 改为校验 `dist` mtime 是否真的变化；未变视为被跳过，30s 后重试，最多 3 次；systemd `TimeoutStartSec=900` 容纳最长路径 |
| 3 | 退化判定要求 `prev.total == len(baseline)` | 基线从 39 扩到 56 后历史里找不到同数基准 → 退化检测**静默失效** | 不再比 total 相等，改记 `from_ratio/to_ratio` 与 `lost` 列表 |
| 4 | 基线域被 `@@||domain^` 放行时 `dig` 返回真实 IP，被算作「失效」 | 触发无效自愈 + 每小时误报 | 新增 `covered_by()` 判定，单列 `whitelist_conflict`，不计 fail、不触发自愈 |

附带改进：dig 并发 `min(16, max(4, 基线数))` 自适应；快照新增 `ver`、`whitelist_conflict`、`coverage.domains`；日志行补「冲突 N」。

## 页面合并（/cn-ad 并入 /status）

`/cn-ad` 的核验结论已作为「广告联盟基线」板块合并进 `/status`：

- 桌面：`header-right` 新增 `📢 广告联盟`（紫渐变，指向 `/cn-ad` 完整页）
- 手机：`.m-nav` 新增 `📢 联盟` 锚点（`#sec-cnad`）
- 板块位置：`规则管理` 之前，复用页内 `.sec / panel / h3` 浅色主题；三栏 = 核验结果（基线生效 / 业务误杀 / 白名单冲突 + 覆盖率条）/ 规则覆盖来源 / 失效与异常
- 数据源：`GET /api/cn-ad-health`（每 60s 刷新，页面隐藏时暂停）
- 折叠、隐藏、锚点高亮沿用页面原有 `initSecCollapse` / `initSecHide`（自动纳入 `.sec` 集合）
- `/cn-ad` 右上角加「← 状态面板」互链

## 回滚（v8 与合并）

```bash
# 审计回滚到 v7
cp /root/agh-bak-archive/cn-ad-net-audit.bak-v7-<ts> /usr/local/bin/cn-ad-net-audit.py

# status.html 回滚
cp /root/backups/status.html.bak-cnadmerge-<ts> /var/www/dns-page/status.html

# status-server 回滚（去掉 /cn-ad 返回链接）
cp /root/backups/status-server.bak-cnadlink-<ts> /usr/local/bin/status-server-py3.py
systemctl restart status-server
```

