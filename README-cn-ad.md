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


## v9 全链加固（2026-11-21 第二轮）

审计 v8 只修了"读"的一侧，v9 把"写"的一侧（构建脚本）也理顺，并补上前端与发现脚本的缺陷。

### 构建链（build-and-refresh.sh v2 / refresh-agh-filters.sh v3）

| 缺陷 | 后果 | 修法 |
|------|------|------|
| 跳过路径（内存不足、构建锁被占）只 `exit 0`，无任何痕迹 | 审计只能靠 mtime 猜，猜不准就是"假成功" | 跳过时写 `state/build-skip.json`（`reason: locked/mem`）；真正开跑先删戳，审计可精确区分"跳过"与"跑过但失败" |
| `refresh-agh-filters.sh` 不校验返回码与返回体，400 也当成功 | dist 已更新但 AGH 没重载 → 规则不生效却报构建成功 | 校验 HTTP 200 + 无 `"error"` 字段，失败 `exit 1`；builder 显式判断并写"规则未生效"日志 |
| 白名单净化 9 条串各跑一次 `grep`（每次全量读 12MB，9 次 IO） | 构建慢、IO 浪费 | 合并为单次 `grep -vF -f` 列表，1 次读盘 |
| 刷新日志 `tail -200` | 两小时前的失败记录就没了 | 提到 2000 行 |

> 实测踩坑：把 `whitelist` 参数化成 `0/1` 会被 AGH 拒（`cannot unmarshal number ... type bool`，HTTP 400）。必须是 JSON bool `false/true`——这正是"刷新失败显式报错"机制当场抓出来的。

### 审计 v9

- 自愈成功判据 = `dist` mtime 变化 **且** 复检有改善；只变了 mtime 但复检无改善 → 记"自愈无效"，不谎报成功。
- 自愈无效时**释放冷却戳**，下一轮（1 小时后）可再试；配 `HEAL_MAX_TRY_PER_DAY=3` 日上限，防上游真塌方时每小时连撞 3 次重建。
- 自愈历史独立持久化到 `state/cn-ad-heal.json`（last_ts/last_ok/last_reason/day_fail/runs），不再因删冷却戳而丢失"上次自愈"。
- 构建超时 300s→420s（refresh 两次 curl 各 60s，加上构建 160s 已逼近旧上限）。
- `build-skip.json` 参与判断，跳过时 20s 后重试，不算失败。

### 前端

- `/status` 广告联盟板块：接口 `ok:false` / `data:null` 时置灰为「快照读取失败」「暂无健康快照」，**不再显示"全部生效"**（此前会假绿）。
- `/cn-ad`：`row()` 全字段 HTML 转义（新增 `esc()` 与 `rowRaw()`，仅"关键宽规则"的富文本走 raw 且内部已转义）。此前构造型域名可注入 `<img onerror>` 并执行。

### 发现脚本 v6

- 候选快照改为 `.tmp` + `rename` 原子写，页面读不到半截 JSON。
- 进度计数加 `threading.Lock`（原来多线程裸 `+=`）。
- 修掉 v6 首次上线自带的命名冲突：计数器字典 `cnt` 被 `for host, cnt, target` 覆盖 → 写快照报 `'int' object is not subscriptable`（已改名 `prog`，全量 22648 域实跑通过）。


## 第三轮加固（2026-11-21 晚）

### agh-postcheck.sh v2（复检链闭环）

- 旧版把构建"跳过"（锁被占/内存不足）当重建成功：`rebuild_rc=0` 但什么都没跑，若复检碰巧通过就记"闭环"。
- v2 读 `build-skip.json`（时间戳须新于本轮开始），跳过则 30s 后重试，最多 3 次；未闭环日志带 `built/last_reason`，不再含糊。
- 红回路验证：假构建恒写 locked 跳过戳 → 旧版只试 1 次（RED），v2 试 3 次并记原因（GREEN）；真成功路径只跑 1 次不过度重试（GREEN）。

### 审计 v9.1（自愈状态一致性）

- 修"重建跑了但复检无改善"时 `cn-ad-heal.json` 仍记 `last_ok=true, day_fail=0` 的矛盾：现在同步记失败、计入日失败上限（3 次/日）、释放冷却供下轮再试。否则上游真坏时会**每小时空转重建**且账面全是成功。
- 自愈成功后重测 12 个业务域误杀（重建可能改变误杀面，不能沿用重建前旧结果）。
- 红回路验证：打桩 dig 让基线域永不生效 + 假构建 touch dist → v9 账面 last_ok=true（RED），v9.1 last_ok=false/day_fail=1（GREEN）；日上限=3 时拒绝再重建（GREEN）。

### 回滚

```bash
cp /root/backups/agh-postcheck.sh.bak-v2-<ts> /usr/local/bin/agh-postcheck.sh
cp /root/backups/cn-ad-net-audit.py.bak-v91-<ts> /usr/local/bin/cn-ad-net-audit.py
```


## 第四轮加固（2026-11-21 晚，并发与通配盲区）

### refresh-agh-filters.sh v4（并发 cookie 竞态）

- refresh 有 4 个调用方：hourly timer、构建链、status-server、备份脚本。旧版共用 `/tmp/agh-refresh.cookies` 且启动先 `rm -f`——并发时 B 会删掉 A 正在用的 cookie，A 的刷新变 401。v3 加了严格校验后，这种竞态会从"静默"升级成"构建链假失败"。
- v4：cookie 改 `mktemp` 独立文件 + `trap EXIT` 清理。红回路（假 curl 注入时序）：旧版 A exit=1（RED），v4 双 0（GREEN）；生产真并发 ×2 亦 0/0，无 cookie 残留。

### 审计 v9.2（白名单通配盲区）

- 产物白名单实测有 **160 条通配**（`*.fqnovelpic.com`、`tnc*.snssdk.com` 等），旧版 `load_allow` 跳过一切含 `*` 的条目 → 被通配白名单放行的基线域会被判"失效"并**触发无效自愈重建**。
- v9.2：`load_allow` 返回（精确集, 通配表），新增 `allow_covered()` 按域+父域逐级做 fnmatch，并兼容 `*.base` 的裸域语义。红回路：`@@||*.zzwx.example^` 放行基线域 → v9.1 fail=1/自愈 1 次（RED），v9.2 conflict=1/自愈 0 次（GREEN）；v9.1 的自愈一致性回路复跑仍绿（无回归）。

### 回归

- 真并发 refresh ×2：0/0；audit v9.2：56/56 生效 0 误杀 0 冲突；完整构建 17 项回归全过 exit=0（2m32s）。

### 回滚

```bash
cp /root/backups/refresh-agh-filters.sh.bak-v4-<ts> /usr/local/bin/refresh-agh-filters.sh
cp /root/backups/cn-ad-net-audit.py.bak-v92-<ts> /usr/local/bin/cn-ad-net-audit.py
```


## 2026-11-21 第五轮：正则覆盖盲区修复 + 首次真实故障演练

### BUG（审计 v9.2 → v9.3）
- 覆盖面判定只编译含 `ads?` 字面量的那一条正则；产物实测有 57 条正则，
  被其它正则挡住的基线域会被记"无覆盖"→ 每小时假告警（dig 明明已拦）。
  红回路 ops/harness_regex_cov.py：非 ads? 正则覆盖 → v9.2 none=1/ALERT（红）→ v9.3 regex=1/ok（绿）。
  修复：load_dist 收集全部正则（上限 256），classify 逐条匹配；快照 dist.regex 改为条数。
- refresh v4.1：日志截断临时文件改 mktemp，四方并发截断不再互踩。

### 真实故障演练（首次在生产走通"告警→自愈→恢复"全链）
1. 备份清单/快照/账本到 /root/drill-20261121-193338
2. 注入 ||zz-drill-1121.example^ 进基线（产物无对应规则，dig 未拦）
3. 跑审计：判失效 1 → 触发自愈 → 重建 159s（17 项回归全过）→ 复检 57/57 → dig 0.0.0.0
   heal.json 正确记账：last_ok=true / runs=1 / day_fail=0；全程 2m40s
4. 还原清单 → 重建 exit=0 → 审计 56/56 / 规则 538670 恢复原值 / 无演练残留
5. 自愈冷却戳在位（1 小时内不重复重建）；failed 单元 0

### 回滚
    cp /root/backups/cn-ad-net-audit.py.bak-v93-<ts> /usr/local/bin/cn-ad-net-audit.py
    cp /root/backups/refresh-agh-filters.sh.bak-v41-<ts> /usr/local/bin/refresh-agh-filters.sh
    cp /root/drill-20261121-193338/cn-ad-networks.txt /opt/agh-builder/local/cn-ad-networks.txt

## 2026-11-21 第六轮：接口报错脱敏 + 复检清单并发加锁

### BUG 1（status-server，公网信息泄露）
- /api/cn-ad-* 快照读取失败时把 str(_e) 原样回给公网，暴露服务器内部路径
  （如 /opt/agh-builder/state/...）与系统错误细节；/api/ai-discovered 同样。
- 红回路 ops/harness_api_leak.py（ast 抽真实 do_GET 分支原文执行）：
  旧版 error 含内部路径（红）→ 新版统一 "snapshot unavailable"，细节只进 stderr（绿）。
- 顺手收紧：/api/cn-ad- 前缀下只认 health/candidates 两个端点，其余回 not found
  （旧版任意后缀都会误读 candidates 文件）。

### BUG 2（postcheck v2.3 → v2.4，并发竞态）
- append_unique 先读后追加无锁：定时器与 wrapper 重试并发时重复写 wan/hardblock。
  生产实锤：hardblock.txt 存在历史重复条目 app-i-csj.y2002.com（已去重，备份在案）。
- 红回路 ops/harness_append_race.py（20 线程并发+放大竞态窗口）：
  旧版 200 行/190 重复（红）→ v2.4 同一 fd flock 排他读+追加，10 行/0 重复（绿）。
- 生产实跑 postcheck v2.4：545+134 域全过 rc=0。

### 部署核验
- status-server.service 重启 active；health/candidates 接口 ok；/cn-ad 200；
  bogus 端点回 not found；failed 单元 0。

### 回滚
    cp /root/backups/status-server.bak-sanitize-<ts> /usr/local/bin/status-server-py3.py && systemctl restart status-server
    cp /root/backups/postcheck.py.bak-v24-<ts> /opt/agh-builder/postcheck.py
    cp /root/backups/hardblock.txt.bak-dedup-<ts> /opt/agh-builder/local/hardblock.txt

## 2026-11-21 第七轮：检测原语 /api/check 专项（脱敏补全 + AGH 会话池）

### BUG（公网 5xx 泄露内部拓扑，7 处）
- /api/check、/api 总出口、report、appeal、ai-exemption-remove、custom-rules 增/删
  异常时把 str(e) 原样回公网（含 http://127.0.0.1:3000 内部地址端口）。
- 红回路 ops/harness_check_leak.py（ast 抽真实分支+打桩抛错）：
  旧版 error 含内部 URL（红）→ 统一 _pub_err() 脱敏为 "internal error"，细节进 stderr（绿）；
  静态断言全文件 'error': str( 残留 0。第六轮 cn-ad 回路复跑仍绿。

### 优化（AGH 登录风暴 → 会话池）
- 旧版 agh_check 每查一个域重新登录一次 AGH：复检一轮 679 域 = 679 次登录；
  且会话 401 时直接抛错不恢复。
- 红回路 ops/harness_agh_login.py（urllib 打桩计数）：
  50 次查询旧版登录 50 次（红）→ 会话池（上限8，取/还）后 1 次（绿）；
  中途 401 旧版抛错（红）→ 自动重登一次成功（绿）。
- dig CNAME 探测加 +time=2 +tries=1，避免默认 3 次重试拖满超时。
- 生产实测：复检 679 域 15.9s → 冷池 11.1s → 热池 3.6s（约 4.4 倍），
  AGH 登录次数 679 → ≤10/轮；广告域/业务域/非法域判定回归全对，failed 单元 0。

### 回滚
    cp /root/backups/status-server.bak-pool-<ts> /usr/local/bin/status-server-py3.py && systemctl restart status-server
