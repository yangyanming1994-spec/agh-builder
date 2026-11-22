# unbound-cn-sync v2（2026-11-21 第八轮：转发分流同步卡死修复）

## 架构事实（本轮取证纠正）
- unbound 为「转发为主、递归兜底」：1379→1161 个 forward-zone（`forward-cn.conf`，
  根区 `.` → 8.8.8.8；国内区+流量热点 → 119.29.29.29 / 119.28.28.28 / 182.254.138.143@10001），
  全部 `forward-first: yes`（转发失败才回退完整递归）。
- `unbound-control list_forwards` **显示格式省略 @port**（实测加 @10001 测试区仍只显示 IP）。
  此前「运行态丢端口」为误判，运行态端口一直正确；首查慢的真因是每区首次接触的
  forwarder 探测 + DNSSEC 密钥链递归成本，稳态 18–40ms。
- AGH 层：upstream 仅 127.0.0.1:53；119.29/119.28 为 bootstrap；
  119.29/119.28/182.254:10001 为 fallback（unbound 整体失效时兜底，日均百余次）。

## 真 BUG 与修复（v2）
1. **同步永久卡死**：删除数护栏固定上限 300 与防骤降合并护栏 30% 口径不一致。
   1379 zone 库正常 churn 308（22%）绕过合并护栏却撞固定上限 → 每轮回滚放弃 →
   旧库不刷新 → 下轮 churn 仍 >300 → 永久卡死（11-21 18:17 轮实证）。
   修：上限改 `max(300, int(len(have)*0.3))`，小库（≤1000）保留原双口径，灾难缩水防护不回归。
2. **热点统计窗口随轮转减半**：`_parse_querylog` 只读当前 querylog.json，AGH 轮转后
   样本窗口减半放大 churn。修：统计窗口 = 当前 + 上一份 `.1`（实测样本 128000 → 225999 条）。

## 红回路（ops/，可重跑）
- harness_sync_window.py：轮转前后夹具，旧版漏轮转前域（RED）→ v2 双窗口（GREEN）
- harness_sync_guard.py：ast 抽真实护栏 If 节点；308/1379 旧版拦截（RED）→ v2 放行，
  900/1379 灾难缩水新旧均拦截（GREEN）
- 用法：python3 harness_sync_*.py [被测源码路径]（默认 ops/unbound-cn-sync.cur 旧版基线）

## 生产验证（23:44 实跑）
- 增量更新：新增 79 删除 297 失败 0（此前被卡的删除正常放行），29s 完成
- 运行态 1161 == 配置 1161 == LIST 1153 + 8 静态区，三轨一致
- 缓存零损失：rrset 31619 前后一致，msg 24971→24967
- 首查抽查：www.gov.cn 0.04s / www.12306.cn 0.03s，复测 0s（缓存）
- AGH 健康 302 正常；定时机制不变：unbound-cn-sync.timer 每 6h（:17），
  下一轮 00:17 自动验证；cache-save 10min / mem-guard 5min / full-warm 每日 照旧

## 回滚
cp -a /root/backups/unbound-cn-sync.bak-v2-20261121-234420 /usr/local/bin/unbound-cn-sync
（回滚后同步恢复卡死态，但不影响解析服务本身）

## 第九轮：长尾性能优化（2026-11-21 23:55，用户批准范围后执行）
配置4项（/etc/unbound/unbound.conf，备份 unbound.conf.bak-perf-20261121-235533）：
- prefetch-key: no→yes（DNSSEC 钥匙到期前后台续期，削冷验签尖峰）
- serve-expired-client-timeout: 500→200（过期刷新等待长尾 -300ms；旧答案仍受 reply-ttl 30 约束）
- infra-host-ttl: 3600（转发器测速档案 15min→1h，减少周期重探）
- aggressive-nsec: yes（签名域 NXDOMAIN/泛解析链省一次上游往返）
cn-sync v3（备份 unbound-cn-sync.bak-v3-20261121-235533）：增量新增>0 时异步触发
unbound-full-warm.py（限流/内存门控/熔断/CONF指纹幂等），新区首查成本后台预付。
红回路C harness_sync_warm.py：v2 无触发 RED → v3 有新增才触发 GREEN。

验证：checkconf 无错；get_option 四旋钮生效；reload_keep_cache 前后
msg 24909 / rrset 31619 完全一致（缓存零损失）；抽查应答全 NOERROR、复测 0ms；
SERVFAIL=0；AGH 302 健康。本轮同步指纹无变化跳过更新（无新增→不预热，符合设计）。

ECS 警告取证（不改动结论）：unbound 告警 subnetcache 模块数据不享受
prefetch/serve-expired；实测出站包带真实客户端网段（AGH edns_client_subnet 开启）、
上游应答 scope=0 → 答案落主缓存仍享受两旋钮；subnet 缓存命中仅 3 次，影响可忽略。
保留 subnetcache：拆除会使异地客户端拿到错误 CDN 节点。

回滚：cp -a /root/backups/unbound.conf.bak-perf-20261121-235533 /etc/unbound/unbound.conf
&& unbound-control reload_keep_cache；脚本回滚 cp -a ...bak-v3-... /usr/local/bin/unbound-cn-sync

## 第十轮：/16 网段聚合 + 阿里第四票（2026-11-22 00:26，用户批准）
- max-client-subnet-ipv4: 24→16（发出网段上限=/16，上游按/16应答、ECS缓存按/16键聚合）。
  语义纠正：聚合靠 max（发出上限）；min-client-subnet-ipv4 是「拒收过短网段」的接收门槛，
  误改会 REFUSED 客户端查询，保持 24 不动（官方 man 证实）。
- 生成器 CN 组加回 223.5.5.5 作第四票（破腾讯同源集体限速）。历史：11-09 因 p90 3038ms
  移出；本轮复测带ECS 3/3 稳定 83-89ms、不带ECS 1327ms（依赖 always-forward 才可用）；
  再劣化由 infra 测速罚分自动边缘化 + infra-keep-probing 自动复测归队。根区仍仅 8.8.8.8。
- 实证：出站问包 ECS option 前缀位 0x10（/16）；应答 CLIENT-SUBNET scope=16；
  同/16不同/24 第三发 0ms 且 num.query.subnet_cache +1（聚合命中成立）；
  运行态 gov.cn 四票含 223.5.5.5；reload_keep_cache 前后 msg 24762/rrset 31724 一致；
  cn-sync 实跑增量 +4/-0 且 v3 预热触发首次生产生效；AGH 302 健康。
- 已知限制（官方 man 明示）：subnetcache 模块不与 prefetch/serve-expired 交互，
  ECS 答案无预取/过期服务；由 AGH 乐观缓存 + full-warm 分网段键补偿。
- 代价：CDN 节点精度区县级→城市级（/16 同城同节点）；隐私改善（对外仅暴露/16）。
- 回滚：cp -a /root/backups/unbound.conf.bak-ecs16ali-20261122-002639 /etc/unbound/unbound.conf；
  cp -a /root/backups/unbound-gen-forward-cn.bak-ali-20261122-002639 /usr/local/bin/unbound-gen-forward-cn；
  cp -a /root/backups/forward-cn.conf.bak-ali-20261122-002639 /etc/unbound/unbound.conf.d/forward-cn.conf；
  unbound-control reload_keep_cache

## 第十一轮：223.6.6.6 入组 + min 门槛回归修复（2026-11-22 00:31-00:32，用户批准）
- CN 组第五票 223.6.6.6（阿里内冗余；带ECS 3/3 84-89ms，不带ECS 208ms）。
  运行态 gov.cn 五票确认；根区仍仅 8.8.8.8；reload_keep_cache 主缓存零损失。
- **自引回归与修复**：第十轮 max→16 后，无网段客户端查询由 unbound 自生成 /16 ECS，
  低于 min-client-subnet-ipv4=24 接收门槛 → 本地 REFUSED（0ms）。
  修：min 24→0（默认，全接受）。验证：无网段/带/24/带/8 均 NOERROR；/16 聚合命中仍 +1。
  影响面：AGH querylog 102000 条 REFUSED=0（AGH 恒带 /24，真实用户零影响）；
  仅 00:26-00:32 本机无网段探针受影响。教训：max<min 组合非法，改 max 必须同步放宽 min。
- 观察：reload_keep_cache 保留 msg/rrset 但网段(ECS)缓存重建，重载后每 /16 首查冷一次
  （824ms/1.09s 样本），属预期，非常态。
- 回滚：cp -a /root/backups/unbound.conf.bak-minfix-20261122-003230 /etc/unbound/unbound.conf
  && unbound-control reload_keep_cache；生成器/分流回滚见 bak-ali2-20261122-003138。

## 第十二轮：九~十一轮改动审计 + v4（2026-11-22 00:43，红回路纪律）
审计范围：cn-sync v2/v3、unbound.conf 六旋钮、生成器五票。查出 2 个真缺陷（均在新代码内）：
1. **轮转竞态双计**（v2 窗口改动引入）：读取序 [json, .1]，解析期间若 AGH 轮转，
   旧窗口被读两遍 → 热点权重翻倍失真。红回路D harness_sync_rot_race：dup 权重 1000(应500) RED。
   修 v4：序改 [.1, json]，竞态时只漏一个窗口(可接受)不双计。GREEN dup=0/old=500。
2. **保缓存重载后无预热**（v3 缺口）：reload_keep_cache 保留 msg/rrset 但重建 ECS/infra 缓存，
   成功路径不预热 → 每 /16 首查冷 0.8-1.1s 由用户承担。红回路E harness_sync_warm_reload：
   成功路径预热=0 RED。修 v4：成功路径也异步限流预热（兼补 ECS 无 prefetch 缺口）。GREEN 1/1。
审查干净项：护栏数学(小库保护保留)、预热仅新增触发、max16/min0 合法组合、
aggressive-nsec 负应答 NXDOMAIN 正确(实测)、五票 infra 账本阿里 rtt 268-365 自然劣后于腾讯
(备份票角色符合设计)、conf.d 的 .bak 不被 include、AGH fallback 未动(用户偏好)。
生产回归：v4 实跑增量 +0/-46 正常放行；运行态 1123 == 配置 1123；
缓存 24786/31849 前后一致；AGH 302；五回路(A窗口/B护栏/C新增预热/D竞态/E重载预热)全绿。
回滚：cp -a /root/backups/unbound-cn-sync.bak-v4-20261122-004358 /usr/local/bin/unbound-cn-sync

## 第十三轮：预热机制三缺陷 + v5/v2（2026-11-22 00:54，红回路纪律）
1. **F 指纹永久跳过**（full-warm 旧版）：同指纹+同分片数即 SKIP，无时间/force 维度 →
   v4 重载后预热与 6h(:22) 周期刷新在 CONF 不变时全为 no-op（11-16/17/18 06:22 SKIP 实录）。
   修 full-warm v2：--force 绕过；同指纹仅 3h 内跳；state 清 7 天前键。
   红回路F harness_warm_skip：force/4h前 旧版跳 RED → 新版跑 GREEN；近期重复仍跳。
2. **G 容量余量不足**（潜在）：1168 域 x10 片=292s vs 300s 熔断仅余 2.7%；热点库 420→1168
   持续增长，过 ~1200 域必撞熔断。修：自适应分片使计划时长<=熔断80%。
   红回路G harness_warm_margin：292s RED → 8 片 234s(余量22.1%) GREEN。
   （假设纠正：曾疑 180s 熔断致每轮 abort，日志实证 TOTAL_TIMEOUT=300 且历史轮完整完成，撤回。）
3. **H 手动运行无锁**：systemd ExecStart 有 flock 但手动 python3 直跑绕过 → 可与 timer 重叠写
   LIST/conf。修 cn-sync v5：脚本内 fcntl 非阻塞锁(同路径)，占用即跳过；三处预热触发改 --force。
   红回路H harness_sync_lock：旧版无锁 RED → 占用跳过/空闲继续 GREEN；生产持锁实测跳过日志确认。
生产实证：force 真跑 1122 域 x8 片=8976 对 243.1s 完整完成 abort=False fail0.9%；
紧接无 force SKIP age=1s；持锁手动运行跳过；锁空闲实跑增量 +2/-0 并触发 force 预热；
运行态 1125==配置 1125；缓存无重置；dig NOERROR 0ms；AGH 302。
审查干净项：地址比对用 set(顺序不敏感)、systemd 串行化、内存/失败/超时门控保留。
八回路(A窗口/B护栏/C新增预热/D竞态/E重载预热/F指纹/G余量/H锁)全绿，留 ops/ 作回归资产。
回滚：cp -a /root/backups/unbound-full-warm.bak-v2-20261122-005422 /usr/local/bin/unbound-full-warm.py；
cp -a /root/backups/unbound-cn-sync.bak-v5-20261122-005422 /usr/local/bin/unbound-cn-sync

## 终验轮（2026-11-22 01:0x）：归档脚本轮转竞态（最后一个已知缺陷）

终验扫出唯一失败单元 agh-querylog-archive.service：01:00 恰逢 AGH 轮转、当前 querylog 暂缺 →
脚本读失败即 exit1 → unit failed；且只读当前文件，两轮之间被轮转进 .1 的尾段永久丢档。
v2 修复：旧→新→旧三遍读 + 本轮 seen 集合去重（游标只作起点过滤、取最大值；
第一版用游标跨遍去重被回路 c 证伪：轮转后第三遍读到更旧时间戳会被滤掉丢档）+ 单遍失败非 fatal、三遍全失败才 exit1。
红回路 I（harness_archive_rot）三场景：缺失当前 fatal/尾段在 .1 丢档/中途轮转丢档，旧全红新全绿。
生产实证：手动 rc=0 补回 8000 条丢尾、游标推进 01:01:12；reset-failed+start 后失败单元清单 0。
备份 querylog-archive.bak-v2-20261122-010413。
终验其余全绿：三脚本真身==修复版、九回路全绿、运行态==配置 1125、缓存 24878/31925、
三类解析 NOERROR/NOERROR/NXDOMAIN、近 3000 条 fallback 0%、无新告警/熔断/回滚。

## 第十五轮（2026-11-22 01:1x）：全量运行代码盘点 + 六缺陷修复

盘点：自定义 service/timer 全列映射脚本；已审计四脚本与镜像哈希一致（无漂移）；
未审计面=两常驻 daemon（hot-warm-v4、upstream-keepalive）+ 十余 timer 小脚本。
修复（红回路先红后绿，harness 存 ops/r15/）：
- H1 hot-warm 瞬态超时误剔热对（实测~1次/分钟）→ 仅确定性非NOERROR才剔，瞬态保留+退避。
- H3+H5 轮转时 mtime 竞态误选 .1（55MB 重读）+ 工作集清零（实测 5000→2155）→ 当前文件优先；
  轮转补读 .1 尾段、工作集与计数保留。
- H4 walk 逐行抢锁 → 整批一次锁（20000 行锁获取 20000→1）。
- H6 刷新率恒=QPS上限=桶饱和、18% 刷新落空 → 默认 QPS 8→12（env 可覆盖）。
- K1 keepalive 启动时解析一次配置/域名库 → 00:26 入组的阿里两票 84h 从未被保活
  （60s 汇总实证仅4键）→ v2 周期300s跟随+worker差量增删；部署后汇总6键阿里 q=12 ok=12。
- S1 unbound-stats 命中率分子缺 subnet_cache（ECS 早已启用）→ 恒0.1~3% 触发<5%慢性误报
  （alert.log 23:15/00:15 实证）→ 分子=cachehits+subnet_cache，首跑 43.0% 无误报。
- B1 backup-core 清单缺 /etc/unbound（1125区配置灾备即丢）→ 补入；实跑包内含
  unbound.conf 与 forward-cn.conf（1573文件/6.3M）。
证伪撤回两条：①「AGH querylog 写盘停滞」实为 size_memory=2000 批 flush（3~6分钟一批，
  fd 间歇为0正常）；发现前已执行一次 AGH 重启（无害：unbound 缓存不受影响，AGH 缓存冷一次）。
  ②「hot-warm 令牌桶 lead 无界」实受 worker 数上限（≤8s），不成立。
溢出收益：腾讯保活探针 104/128ms→66/68ms、本地预热 243→34ms（hot-warm v5 覆盖更全）。
遗留披露：hot-warm 刷新率在12仍饱和（工作集2443、短TTL占比高），观察命中率趋势再定
  ratio/QPS 二选一；worker 到期全表扫描 CPU 3.6%（可改堆但收益小不动）；seed 日志「尾部0字节」
  为_walk后计算_的文案缺陷（不影响行为）；agh-auto-update 04-06窗口无人值守升级AGH主程序
  （风险项待用户决策）；unbound-mem-guard 阈值1100MB 动作是 flush_zone .（全清缓存，当前RSS172MB
  远低于阈值，属死代码级保险）；status-server-mem-guard 无冷却（持续超阈会每5分钟重启）；
  AGH RSS 525MB 接近告警阈600MB（过滤规则53.8万条内存占用，观察）。
备份后缀 .bak-r15-20261122-011819；AGH yaml 备份 .bak-qlog-20261122-011353。

## 第十六轮（2026-11-22 01:3x）：剩余未审计面清尾 + 四缺陷修复

清尾范围：agh-update/backup-config/backup-querylog/cache-load/agh-monitor/agh-build-guard/
agh-postcheck/agh-config-guard/agh-clients-tick/kms-stats/alert.sh/sofmed_observe/adopt 路径扫描。
修复（红回路先红后绿，harness 存 ops/r15/）：
- Q1 backup-querylog 每日误删 hot-warm 的 .pos 游标（备份目录里 46 字节 .pos.gz 实证）、
  删除前不校验备份、.1 无消费宽限 → v4：不碰 .pos、gzip -t 校验后才删、.1 保留 6h。
- M1 agh-monitor 兜底拉起失败只写本地日志=静默宕机 → 接 alert_throttled(agh-down/agh-recovered)。
- U1 agh-update 健康检查 dig @127.0.0.1 实测的是 unbound（端口归属实证：unbound 127.0.0.1:53、
  AGH [::]:53）→ unbound 瞬时无答案会错误回滚好的 AGH 升级 → 改打 @::1。
- B2 backup-config 清单同样缺 /etc/unbound → 补入（实跑包内 25 个 unbound 条目）。
证伪/关闭三条：①sofmed_observe「缺失」实为脚本目录导入（/usr/local/bin/sofmed_observe.py 存在）；
  ②cn-ad-net-discover LAST=n/a 非死定时：单元 11-21 14:42 创建、OnCalendar=Sun 03:45 weekly，
  今天首跑；①上轮 dump_infra 取错列的 rtt 数字撤回，正确解析阿里 357ms≈腾讯 371ms（K1 收益确认）。
H6 闭环：hot-warm 命中率 82%→90%、刷新率 8.5/s<12 不再饱和，无需再调。
披露待决策：/etc/sysconfig/smartdns-alert 无外部通道配置 → 告警仅本机留痕（箱死即无人知），
  配置 webhook/token 需用户提供；agh-auto-update 无人值守升级仍待决策；WECOM 通道 JSON 未转义
  （当前未启用该通道，休眠缺陷）；backup-clean 注释「core 60 天」与 backup-core 实际 14 天不符（文案）。
回归：四新回路对真身全绿+历史回路全绿；失败单元 0；monitor/backup-querylog/backup-config 实跑通过；
agh-update --check 返回「已是最新」rc0 未触发升级。备份后缀 .bak-r16-20261122-013954。

## 第十七轮（2026-11-22 09:1x-09:5x）：夜间无人值守回归 + 核心链停摆根因修复

夜间 timer 链全跑通取证：失败单元 0；discover 首跑 03:45→03:50:54 实探 23458 域/250.3s/候选 0/rc0；
full-warm v2 首轮无人值守 06:22 完成 8992 对/230.5s/失败率 0.5%/未熔断/已提交；archive 01:40 后 0 错误；
build 08:16/08:46 自检通过；postcheck 三轮通过（545 功能 + 134 广告）；backup-core 05:10 OK 6.4M/1573 文件、
backup-config 05:00 OK 30M/1492 文件 checksum PASS；kms-selfcheck 03:20 正常。

修复四缺陷（红回路先红后绿，harness 存 ops/ 与 ops/r15/）：
- L1（严重·核心链停摆）unbound-cn-sync v5 脚本内 fcntl.flock 与 systemd wrapper 的 `flock -n` 同属
  BSD 锁空间 → wrapper 持锁期间脚本自锁必失败 → **每个 timer 轮都"锁占用"自跳过**。00:17 轮跑在 v4
  故正常，06:17 是 v5 首个 timer 轮 → 国内转发链自 06:17 停摆约 3h（手动跑无 wrapper 所以此前测试全绿，
  属测试与生产不同构的盲区）。→ v6 改 fcntl.lockf（POSIX 锁，与 wrapper 的 BSD flock 独立空间，
  仍防手动/跨实例重叠）。生产同构回归：用与 timer 完全相同的 `flock -n … -c 'python3 …'` 补跑，
  rc=0、写入 1139 域、增量 +37/−15 失败 0、未 reload 缓存保留。回路 harness_sync_lock_self.py。
- F1 full-warm v2 top_ecs() 的 except 统一打 traceback：00:57 轮转瞬间 querylog.json 暂缺属预期，
  功能上已回退基线网段继续跑，但 traceback 被系统自检扫成"异常"告警 → v3 对 FileNotFoundError/OSError
  单独静默回退（一行 [rotate] 提示），其余异常仍留 traceback。回路 harness_warm_rot_quiet.py。
- SC1/SC2 sofmed-system-check.sh 慢性误报：SERVICES 仍含 11-08 已退役的 smartdns；/review.html
  /rules.html 是鉴权页，401 是正确行为却被期望 200 判坏 → v2 移除 smartdns、这两页接受 200|401。
  复跑 bad 为空（仅剩 00:57 那条历史 traceback，24h 窗口过后自然消失）。回路 harness_syscheck_fp.py。
- D3（潜伏）cn-ad-net-discover 有候选时 return 1 → oneshot 单元记 failed（首跑候选 0 未显现）
  → v7 候选是报告产出而非故障，改 return 0。回路 harness_discover_rc.py（dig 桩造候选）。

QPS16 对照实验（已回滚）：hot-warm 工作集顶到 MAX_PAIRS 5000、命中率 90%→86%、刷新 ~11.8/s 疑似
QPS12 饱和 → drop-in Environment=HOT_WARM_QPS=16 跑 28min 稳态对照：命中率 75%（基线 86%）、
均值 62ms（基线 34ms）、有效刷新 9.1/s（基线 11.8/s），**三项全劣**：加压把上游排队拉长反而降吞吐。
结论：删除 drop-in 回到默认 12，瓶颈在上游单查询延迟而非本地令牌桶；不再提 QPS。

证伪撤回三条：①「cn-sync 08:17 缺轮」不成立，schedule 实为 00/06/12/18:17（NEXT=12:17）；
②「hot-warm claim 窗口致 5s 重刷」不成立，成功路径 exp=now+ttl 正确，间隔=ttl*(1−R)，饱和是需求>QPS；
③「discover 被超时杀」不成立，TimeoutStartUSec=0（无限），full-warm 才是 10min。
另：kms-key-sync-apply 03:10 Result=success 且 /root/kms_sync.log 0 字节 = 脚本仅动作时 print 的静默成功，非缺陷。
读审干净：gh_push / sofmed_observe.real / assign-client-region / deploy-cert / adopt append 路径
（原子写+fsync+去重+flock+重试+顶层保护）。
运行态终验：转发区 1147、stats hit_all 66.5%、servfail 0、unbound RSS 173MB、avail 2143MB、load 0.40、
失败单元 0、keepalive 六键 err=0（182.254 自定义出口 TCP+UDP 双条目按用户偏好原样保留）。
遗留待用户决策（沿用上轮）：外部告警 webhook/token；agh-auto-update 04-06 无人值守升级策略。
备份后缀 .bak-r17-*。

### 第十七轮补：微信告警通道接通 + 告警库 v2（额度护栏）

用户澄清"已对接微信"→ 全机核查结论：本机从未配置任何外部推送通道（/etc/sysconfig/smartdns-alert
不存在，全机无 webhook/token 痕迹，bash_history 0 命中）；此前 grep 命中的 weixin/pushplus/ftqq
字样均为广告规则列表里的被拦域名与探测目标域名，非通道配置；ai-bot 是网页 AI 客服代理，与微信推送无关。
用户提供 Server酱 Turbo SendKey → 写入 /etc/sysconfig/smartdns-alert（chmod 600，密钥不入文档/仓库），
ALERT_ENABLED=1、ALERT_PREFIX="[DNS] "。直发与 alert_throttled 两条测试均返回 code=0/SUCCESS。
推送域名解析实测未被自家规则拦截（qyapi.weixin/sctapi.ftqq/pushplus/wxpusher 均正常解析）。

随之发现并修复的真实风险（红回路 harness_alert_quota.py 存 ops/）：
- A1 Server酱免费版每天仅 5 条（官方口径），而告警 key 有十余种、默认节流窗口 1800s →
  故障日普通告警会吃光额度，最关键的宕机告警反而发不出。→ alert.sh v2 加按天额度护栏：
  普通告警上限 ALERT_DAILY_CAP=4，关键告警可动用预留的第 5 条（ALERT_CRITICAL_CAP=5）；
  关键性按 key 自动判定（agh-down/unbound-down/dns-down/cert-expire/backup-fail/system-down/agh-recovered），
  也可由 alert_throttled 第 4 参显式指定；超额只本地留痕并计数，次日首条推送附带"昨日被压制 N 条"。
- A2 所有通道 curl 结果被 >/dev/null + || true 吞掉 → 推送失败（额度用尽/密钥失效/网络不通）完全静默。
  → v2 解析返回码（Server酱 code、TG ok、企业微信 errcode、Bark code），非成功写 [push-fail] 到 alert.log。
- A3 企业微信分支 JSON 手工拼接未转义（上轮披露的休眠缺陷）→ v2 用 python3 json.dumps 生成 payload，
  含引号/换行的告警正文不再产生非法 JSON。
验证：旧版三项全红、v2 三项全绿（额度拦下第 3 条普通告警但放行 agh-down；失败可见；带引号 payload 可解析）；
部署后真实发第 3 条 code=0、alert.log 无 push-fail、计数器=3、agh-down 判定 critical、失败单元 0。
今日额度已用 3/5（两条接通测试 + 一条升级验证），计数器已按实际写入避免超发。
备份 /root/backups/alert.sh.bak-r17-*。若日后告警量变大，可换企业微信群机器人（无条数限制），
alert.sh 已支持 WECOM_WEBHOOK，只需替换配置文件中的通道项。

### 第十七轮补2：无人值守升级可观测（agh-update v3）+ 用户决策

用户决策：维持 04-06 窗口无人值守自动升级（理由：已成功自动升级过一次）。
历史证据：/var/log/agh-auto-update.log 轮转内 2026-11-20 04:17:22「✅ 升级完成: v0.107.80 -> v1.0.0
（DNS 解析正常，备份 /root/agh-backup-20261120-041715）」；当前 v1.0.0，今日 04:17 与 11:17 检查均「已是最新」。

随之发现并修复 A4（真实盲区）：agh-update 真身 grep alert = 0，升级中止/健康检查失败/自动回滚/
回滚后服务仍异常全部只写本机日志，微信通道接通后这些路径依然不会推送 → 无人值守失败无人知。
→ v3 全路径接告警（红回路 harness_update_alert.py 存 ops/，三段 seam 取自真身原文）：
- fail() 按语义分级：含回滚/版本不符/包损坏/解压失败/读不到当前版本 → key agh-update-fail，critical=1；
  下载失败、获取最新版本失败等瞬态 → key agh-update-warn，critical=0，节流 21600s 防刷屏。
- 升级成功 → key agh-upgraded（普通额度），正文含旧→新版本与备份路径。
- 健康检查失败开始回滚 → agh-update-fail critical=1。
- 回滚后服务正常 → agh-rollback-ok critical=1；回滚后仍异常 → 复用 key agh-down critical=1
  （可动用当日预留额度，且与 agh-monitor 共享节流去重，避免同一故障双发）。
- alert.sh 缺失时降级为 no-op，升级流程本身不受影响。
验证：旧版三段 seam 全无告警（红）；v3 两个分支全绿（回滚后仍异常→agh-down|1；回滚成功→agh-rollback-ok|1；
成功→agh-upgraded|0；fail 分级正确）。部署后 bash -n 通过、--check 实跑 rc0「已是最新」未发告警、
当日额度计数器仍为 3、alert.log 无 push-fail、失败单元 0。备份 /root/backups/agh-update.bak-r17-*。

## 第十八轮（2026-11-22 11:2x）：未审面盘点 + 披露项清尾 + 本轮新代码自审

盘点范围：从未读过的 kms-selfcheck.sh / kms_key_sync.py；上轮披露未修项；本轮新写的 alert 额度护栏自审；
logrotate 覆盖全量核验。watchdog.sh 与 capacity-report.sh 做关键动作扫描（告警/重启/删除/冷却路径）通过，
未逐行全读，如实标注。

修复（红回路先红后绿，harness 存 ops/ 与 ops/r18/）：
- K1（严重·会改错生产文件）kms_key_sync.win_prod_to_var 按 dict 插入序做子串匹配，短键先命中：
  「企业版LTSC」「企业版 LTSC 2021」「IoT企业版LTSC」→ 错映射 Enterprise；「企业版NLTSC」→ 错映射 EnterpriseN。
  该脚本由 kms-key-sync-apply.service 每周日 03:10 带 --apply 自动改写 /var/www/dns-page/KMS-Sofmed.bat，
  一旦微软更新 LTSC 密钥且该密钥不在 bat 中，就会把普通企业版的密钥替换成 LTSC 密钥 → 客户端激活失败。
  → v3 改最长优先匹配（sorted by len desc）+ 第二道保险：产品名含 LTSC/LTSB/IoT 时只允许落到 *S 变量，
  否则返回 None 交人工。回路 harness_kms_map.py 11 例：旧版 5 例错映射（红），新版全对（绿）。
  实跑检测：官方 Windows 82 / Office 51，无密钥变更（缺陷属潜伏，尚未触发）。
- K2 KMS 全链路失败静默：kms_key_sync 抓取失败/解析异常/结构异常/验证失败/需人工/部署完成，
  以及 kms-selfcheck 重启后仍失败，全部只写 /root 下日志 → 接统一告警库（结构类 critical=1，其余普通），
  新增 key kms-sync-warn/kms-sync-fail/kms-sync-manual/kms-sync-applied/kms-down/kms-recovered。
  回路 harness_kms_alert.py（抓取失败夹具）：旧 0 次告警（红）、新 1 次（绿）。
- G1 status-server-mem-guard 无冷却无告警：RSS>400MB 时每 10 分钟重启一次面板，泄漏持续则反复中断且无人知情
  → v2 加 600s 冷却（标记 /var/run）+ 重启后 is-active 校验 + 告警（恢复普通/仍挂 critical）。
  回路 harness_memguard_cooldown.py：连跑 3 轮旧版重启 3 次（红）、新版 1 次（绿）。
- G2 unbound-mem-guard 动作过激：只有 1100MB 一档，触发即 flush_zone . 全清缓存（命中率雪崩，
  而第十七轮已证明上游对突发压力敏感）→ v2 分级：≥900MB 只预警不动缓存，≥1100MB 才全清且 critical=1；
  冷却标记从 /tmp 移 /var/run（tmpfiles 清理会让冷却失效）。回路 harness_unbound_memguard_tier.py 两档均绿。
- A5a alert.sh 自审：ALERT_ENABLED=0 或未配置任何通道时仍消耗当日额度 → v3 先探测通道可用性，
  不可用只留痕不扣额度。回路 harness_alert_nochannel.py：旧 used=1（红）、新 used=0（绿）。
- A5b alert.sh 自审：额度"读-判-增"非原子，并发告警会超发 → v3 用 flock 串行化。
  **实现陷阱（回路抓到）**：`allowed=$( … ) 9>lockfile` 的 fd 不会传进命令替换子 shell，
  flock 报 "9: Bad file descriptor" 后静默不加锁；必须把 `exec 9>lockfile` 写在子 shell 内部。
  回路 harness_alert_atomic.py 加"锁必须真生效"断言（stderr 不得出现 Bad file descriptor）：
  v2 并发 8 发 5（红）、错误写法计数偶然通过但锁报错仍判红、修正版并发 8 发 4 且无锁错误（绿）。
- L2 logrotate 遗漏 4 个日志：unbound-full-warm-after-sync.log（每 6h 写）、cn-ad-net-discover.log（每周写）、
  unbound-mem-guard.log、status-server-mem-guard.log → 新增 /etc/logrotate.d/sofmed-r18
  （weekly rotate 8 compress delaycompress missingok notifempty copytruncate su root root），logrotate -d 校验无 error。
- T1 文案两处：backup-clean 注释「core 由 backup-core 按 60 天清理」与实际 14 天不符 → 改 14；
  hot-warm seed 日志「尾部 0 字节」是 walk 后才算 size-off['pos'] 所致 → 先存 tail 再打印。

关闭上轮疑问：cn-sync 日志历史确已轮转（unbound-cn-sync.log-20261122 14418B + -20261115.gz），
06:17 那行是新文件首行，与 L1 停摆定性一致，不存在日志丢失。
回归：8 个新回路对线上真身全绿；部署完整性 diff 四个真身==修复版；KMS 自检实跑 rc0「KMS 正常」；
两个内存守护实跑 rc0 当前不动作；告警库 v3 真实发送 code=0、无 push-fail、当日额度 4/5 无压制；
失败单元 0；stats hit_all 68.0%、servfail 0、RSS 173MB；hot-warm 回到 QPS12 基线 4998 对/命中率 85%/均值 34ms；
dig 百度 6ms、QQ 12ms；cn-sync 下轮 12:17。备份后缀 .bak-r18-*。

## 第十九轮（2026-11-22 11:3x-11:4x）：全量在跑程序审计（51 个脚本）+ 五项修复

范围：按 systemd 单元 ExecStart 全量清点，拉取 51 个在跑脚本（358KB）做静态扫描（scan_r19.py，
10 类高信号模式），命中 47 条线索，再对高危项逐行读。此前几轮只覆盖 DNS 主链，本轮补齐外围。

修复（红回路先红后绿，harness 存 ops/）：
- W1（假成功·死任务）update-cnlist.sh 维护 /etc/smartdns/cn.list 并 restart smartdns，但 smartdns
  2026-11-08 已退役：单元不存在、/etc/smartdns 目录不存在、5353 无监听（均实测）。mv 目标目录不存在必失败、
  探测必失败 → 二次 restart 也失败，而脚本最后一条是 echo，单元每周报 Result=success。
  11-16 04:30 日志实证：「更新完成：109506 行，smartdns 首次探测失败已二次重启」——全是假的。
  唯一消费方是脚本自己（grep 全机确认）。→ 停用 update-cnlist.timer（回滚：systemctl enable --now update-cnlist.timer）
  + 脚本加退役守卫（单元不存在则 SKIP exit 0），双保险防误启用后再假成功。回路 harness_cnlist_dead.py。
- W2 agh-mem-guard 无冷却 + 告警时序错：RSS≥900MB 每轮（每小时）重启 AGH，无冷却；且在 restart 之前
  就发"已自动重启"告警，重启失败时告警内容不实。→ v2 加 1800s 冷却（/var/run 标记）+ 先重启后
  is-active & dig @::1 双校验再告警；恢复发普通告警，未恢复复用 agh-down（critical）。
  回路 harness_agh_memguard.py：连跑 3 轮旧版重启 3 次（红）、新版 1 次且告警在重启后（绿）。
- W3 monitor-healthguard 自愈失败静默：连续 2 次异常即 restart sofmed-monitor，但重启后不校验、不告警、无冷却
  （该脚本背景注释自述"主循环僵死 3 天无人发现"）。→ v2 加 600s 冷却 + 重启后 healthz 校验 +
  恢复/仍失败分别告警（仍失败 critical）。回路 harness_monitor_hg.py。
- W4 update-geocn 更新后不回滚不告警：mmdb 下载与解析校验齐全，但 mv 覆盖旧库前无备份、restart geo-server
  后不校验 → 新库若让服务起不来，归属地功能全挂且无法回滚、无人知情。→ v2 覆盖前 cp .prev、重启后
  is-active 校验、失败自动回滚旧库与版本号并重启、回滚成功/失败分别告警（均 critical）。
  回路 harness_geocn_rollback.py：旧版坏库留线上且静默（红）、新版回滚到 OLD-LIB 并告警（绿）。
- W5 sofmed-auto-update 回滚静默（**非休眠**：/etc/sofmed-auto-update.conf 实测 AUTO_APPLY="sing-box mtg"，
  这两个组件真的会在 04-06 窗口自动升级）：版本不符中止、服务未 active 回滚、端口未监听回滚三条路径
  只写日志。→ v2 三条路径均接 critical 告警。回路 harness_sofmed_update_alert.py。

假阳性关闭（扫描命中但核实无缺陷）：
- audit-reverse.py / scan-fp.py / ai-bot audit.py / healthcheck.py 的 fcntl.flock：对应单元 ExecStart
  均无 flock wrapper（实测），不存在第十七轮 L1 那类同锁空间自冲突；dynamic-ad-discovery.py 内部无锁、
  仅 wrapper 持锁， likewise 无冲突。
- cert-reload.sh：已有 nginx/3proxy 与 AGH 独立 md5 标记、失败告警、失败不写标记下次自动补推，设计完善，不改。
- sofmed-auto-update 的 flock 用法正确（exec 9> 在主体内、flock -n 9），与第十八轮 alert.sh 的错误写法不同。

已识别但本轮不动（附理由，避免把挂起变成崩溃）：
- 7 处 subprocess 调用无 timeout（health-loop.py 2、soa-collect.py 1、ai-bot/server.py 3、
  upstream-latency-probe.py 1）：加 timeout 必须同时加 TimeoutExpired 处理，否则把"挂起"变成"抛异常崩溃"，
  需逐个设计降级路径，单独一轮做。
- 4 处 /tmp/*.lock 与 2 处 except: pass（sofmed-monitor.py:152、swallowed-daily.py:18）：低危，
  /tmp 锁被 tmpfiles 清理只影响极端并发；静默吞异常需先判定是否属预期路径。

验证：5 个新回路对旧版全红、新版全绿；部署后 bash -n 全通过；cnlist 实跑 SKIP rc0；
agh-mem-guard 实跑只记录 RSS=524MB 未动作；monitor-healthguard 实跑 failcount 归 0；
geo 实跑 SKIP 当前已是最新 v26.4.19；sofmed-auto-update --check rc0 零改动（报告 nginx/3proxy 落后但不在白名单）；
update-cnlist.timer 已 disabled；失败单元 0；avail 2049MB、load 0.61；当日告警额度 4/5、无 push-fail。
备份后缀 .bak-r19-*。

## 第二十轮（2026-11-22 12:0x-13:2x）：清尾六项 + 每周自动体检上线

一、补上轮欠账：watchdog.sh（21KB/493 行）逐行全读，发现两处真缺陷
- X1（严重·自愈静默失效）AGH 假死探测目标取自 /usr/local/bin/local-ip，而该脚本最终兜底是
  `echo "${V:-127.0.0.1}"`。127.0.0.1:53 上是 unbound 不是 AGH（AGH 持 [::]:53）。
  路由抖动走到兜底时，"探 AGH"变成"探 unbound"：AGH 真死也探得通 → 一次都不会重启，且无任何告警。
  → v2 回落识别（空/127.*/::1 一律改探 ::1），并新增 agh_probe_target_ok：回落模式下先用
  ss -lntu 确认 [::]:53 真有监听才探；无监听则只记 ALERT_PROBE_CFG + 告警，绝不误重启。
  回路 harness_wd_probe_fallback.py 两场景：A(AGH死/unbound活/回落) 旧版 0 次重启（红）→ 新版正确重启（绿）；
  B(回落且::1无监听) 新版只告警不重启（绿）。
- X2（中·证据自毁）log() 每写一条就 `tail -100` 截断自身。面板 SELFHEAL 只读展示 watchdog.log，
  auto_ack_alerts 也依赖历史 ALERT 行 → 崩溃告警稍后就被自己抹掉，自动确认永远等不到依据。
  → 上限放宽到 5000 行（轮转仍由 logrotate.d/sofmed-custom 负责）。
  回路 harness_wd_log_retain.py：写 1 条崩溃告警 + 150 条普通日志，旧版剩 100 行且告警消失（红）→ 新版 151 行保留（绿）。

二、上轮"识别未修"的挂起风险，本轮按解释器差异逐个修（不是简单加 timeout）
- X3 known-domain-protect.py:156、X4 audit-reverse.py:231 均为 **python2**（无 subprocess.run(timeout=)）。
  → Popen + deadline 轮询（默认 1800s，env KDP_BUILD_TIMEOUT / AR_BUILD_TIMEOUT 可调），
  超时 **os.killpg 杀整个进程组**（只 kill 父进程时 sleep/curl 孙进程仍持有 stdout 管道，communicate 继续阻塞
  —— 这是回路实测出来的，第一版补丁就踩了），并接 kdp-build-timeout / kdp-build-fail / ar-build-timeout 告警。
  回路 harness_build_timeout.py：旧版 >15s 仍挂起（红）→ 新版 4.1s 终止并告警（绿）。
- X5 /root/ai-bot/server.py 两处 Popen.communicate() 无超时，会永久挂住 HTTP 处理线程。
  → _comm() 带超时 + start_new_session=True（线程服务不用 preexec_fn）+ 超时 killpg，交既有 except 降级为 None。
- X6 health-loop.py 三处（systemctl is-active / ss -lntp / crontab -l）与 soa-collect.py 一处
  （systemctl --failed）无超时。→ health-loop 新增 _run_t() 统一带超时 + 进程组清理；soa 加 timeout，
  失败计数按既有 except 降级 -1。回路 harness_subproc_timeout.py 三个 seam：旧版全挂起（红）→ 新版 3.1s 降级（绿）。

三、核实后关闭的假阳性（不做无谓生产改动）
- /tmp/*.lock 被 systemd-tmpfiles 清理（tmp.conf: `v /tmp 1777 root root 10d`）：四个锁均以 open(...,'w')
  或 exec N> 打开，每次运行都会截断刷新 mtime，10 天 age 规则够不到活跃锁 → 不构成并发缺陷。
- sofmed-monitor.py / swallowed-daily.py 的 except：前者是探测失败返回 (False,-1) 的预期路径且已有
  [silent-exc] 上报机制，后者是 --days 参数解析兜底 → 均属预期降级，不改。
- health-loop 的 smartdns 检查是"零残留"断言（带 timeout=10），不是查已退役服务的假阳性。

四、定期优化：新增每周自动体检（只读，不改任何生产配置）
/usr/local/bin/sofmed-self-audit.sh + sofmed-self-audit.timer（Sun 06:30，Persistent=true，
RandomizedDelaySec=300，TimeoutStartSec=600，Nice=10；首次触发 2026-11-29 06:33）。
十类检查，全部是历轮真实踩过的缺陷类型：
 failed 单元 / timer 指向不存在脚本或仍引用已退役对象(W1 类) / 会重启服务却未接告警库 /
 >50MB 日志未纳入 logrotate / core+config 备份新鲜度(>48h 警告, >120h 严重) / 证书剩余天数(<15 警告, <3 严重) /
 磁盘与可用内存水位 / DNS 双活(@127.0.0.1=unbound、@::1=AGH) / alert.log 推送失败 / 规则数漂移(±20%)。
结果写 /var/log/sofmed-self-audit.log（新增 logrotate.d/sofmed-r20，weekly rotate 12）与
/var/lib/sofmed-self-audit/last.txt；有严重项走 critical 键 system-down，否则普通键 self-audit-weekly
（节流 6 天，每周 1 条，免费额度 5 条/天足够）。
验收回路 harness_self_audit.py 是判别式的：坏环境夹具 13 类缺陷必须全抓、干净环境必须零误报、
推送分级必须正确；对空实现跑为红，证明回路能失败。
**该回路抓出新脚本自身两个真 bug**（已修）：
 (a) `exec 9>"$LOCK" 2>/dev/null` —— exec 不带命令时重定向永久生效，把整个脚本 stderr 丢进黑洞；
 (b) `grep -c ... || echo 0` —— 零匹配时 grep 本就输出 0 且退出码 1，再补一个 0 得到 "0\n0"，
     算术展开致命错误使非交互 shell 直接退出，干净环境永远发不出周报。
首次实跑还暴露两处探测器误报（已修并写进夹具）：config 备份实际落 /root/backups/config-*.tgz（无子目录）；
已加退役守卫的 update-cnlist.sh 不该算告警盲区。

五、部署与回归（全部实测）
备份后缀 .bak-20261122-r20；bash -n / python2 -m py_compile / python3 -m py_compile 全通过；
soa-collect 与 ai-bot 重启后均 active；周体检复跑「严重 0 / 警告 0」；watchdog 实跑 rc=0；
health-loop 全链路 22 项 PASS、0 项失败、rc=0；audit-reverse --dry-run rc=0；
dig @127.0.0.1 与 @::1 均正常解析；failed 单元 0；avail 2104MB、load 0.29。
回滚：cp /root/backups/<name>.bak-20261122-r20 回原路径；停用周体检 systemctl disable --now sofmed-self-audit.timer。

### 第二十轮·完成度核验补记（13:2x-13:3x）：核验本身又抓出 3 个真问题

方式：不靠记忆，拉线上真身与补丁做 md5 逐一比对（r19 五项 + r20 七项，12/12 SAME），
再用修正后的 ExecStart 解析拉全 59 个在跑脚本复扫（scan_r20.py）。

- X8（严重·新代码静默空转）sofmed-self-audit.sh 第 2 项用 `systemctl show -p ExecStart --value`，
  但本机 **systemd 219 不支持 --value**（实测 `systemctl: unrecognized option '--value'`，rc=1），
  该项全程取空 → 最有价值的"死任务/退役引用"探测在本机上从未真正运行。
  且 `path=` 取到的是解释器 /bin/bash 而非脚本本身，即便支持 --value 也判不准。
  → 改为 `systemctl show -p ExecStart` 并从 argv[] 中取真正的脚本路径（.sh/.py 或 /usr/local/bin、/opt 下），
  path= 仅作兜底；grep 加 -I 跳过二进制。真机证明：59 个 timer 中 52 个成功解析出脚本（其余为二进制/内联命令）。
  **回路桩此前用了带 --value 的假输出，属于桩不真实导致的假绿**；已把桩改成真实 219 行为
  （遇 --value 报错退出、输出 ExecStart={ path=/bin/bash ; argv[]=... }），旧版复红、新版转绿。
- X7（高·与 X4 同类）scan-fp.py:225 重建 Popen.communicate() 无超时（py2），卡住则单实例锁挡死后续轮次，
  误伤白名单静默不生效。→ 与 X4 同方案：deadline 轮询 + killpg + fp-build-timeout 告警。
  回路 harness_build_timeout.py fp：旧版 >15s 挂起（红）→ 新版 4.1s 终止并告警（绿）。
- X9（新探测器误报）retired-ref 原按"文件里出现 smartdns/5353"判定，把 backup-config.sh / backup-core.sh
  误判为退役引用——它们只是把历史路径列进备份清单/注释，并不操作退役对象。
  → 收紧为"真的操作"：systemctl (restart|start|reload|enable) … smartdns、-p 5353、写入 /etc/smartdns。
  夹具补上"被 timer 引用的备份类脚本"（此前该脚本未被任何 timer 引用，等于没覆盖，控制实验不红），
  旧 grep 复红、新 grep 转绿；真机 13:26 两条误报 → 13:29 归零。

复扫剩余 46 条线索的定性（未修部分均给出理由，不做无谓改动）：
- 已有界，属扫描器误报：status-server-py3.py 6 处 Popen 均走自带 _communicate(p,timeout)（超时 kill + 有界回收）；
  unbound-full-warm.py:93 与新增 _alert 的 bash 调用由 alert.sh 内部 curl 超时兜底；
  cn-ad-net-audit.py:45 dig 带 +time=3 +tries=1；update-geocn.sh:26 命中的是 `command -v curl` 存在性检查。
- 本地短命令，无挂起面：cp/pgrep 类若干（audit-reverse 3 处 cp、kms_key_sync:191 cp、upstream-latency-probe:167 pgrep）。
- 已评估低危：/tmp 锁 4 处（每次 open(...,'w') 截断刷新 mtime，10d tmpfiles 规则够不到）；
  静默吞异常 3 处（探测失败返回 False/-1 的预期降级，且已有 [silent-exc] 上报）。
- 仍未处理（下轮范围，已明确）：unbound-upstream-keepalive.py:100 `tail -n 4000 querylog` 的 communicate() 无超时；
  ad-feature-lib.py:49 fire-and-forget Popen 不回收构建结果；ai-bot/server.py:258 Popen 未逐行核；
  status-server-py3.py 4 处 shell=True 抽查取值来自内部上游清单而非 HTTP 入参，完整数据流核实待做。

终验：scan-fp py2 编译通过、--dry-run rc=0；周体检「严重 0 / 警告 0」；health-loop 全链路 0 项失败；
failed 单元 0；dig @::1 正常；avail 2054MB。备份 .bak-20261122-r20 / -r20b。

### 第二十一轮：四项遗留风险收尾（13:4x-13:5x）

上一轮复扫留下的 4 项，逐项定性并处理；全部先写回路复红、再转绿。

**Y1 keepalive 读 querylog 无超时（真缺陷）**
unbound-upstream-keepalive.py 是常驻服务（Type=simple），`tail -n 4000 querylog` 的 `communicate()` 无超时。
querylog 在 AGH 批 flush、磁盘抖动或文件被轮转时，tail 可能长时间不回 → 整个保活循环挂死，
上游链路预热与 never-stop 查询全停，且无任何告警。
→ v2：`start_new_session=True` + `_comm_bounded(p, 10s)`，超时 `killpg` 整组并降级返回已采集部分。
回路 `harness_ka_tail_timeout.py`（PATH 上 tail 睡 30s）：旧版 15s 仍未返回（红），新版 2.0s 返回并记 swallow（绿）。

**Y2 ad-feature-lib 触发构建不回收结果（真缺陷）**
`_trigger_build()` 在 status-server 未运行时 `Popen(build-and-refresh.sh)` fire-and-forget：
构建失败 rc 不可见、卡住会留孤儿进程，而单元 TimeoutStartSec=30min 也不会立刻兜住。
新增特征可能静默不生效，用户侧只表现为"该拦的没拦"。
→ v2：`wait(timeout=1500s)` 回收 rc；超时 `killpg` 整组并关日志句柄；
失败/超时分别走 `afl-build-fail` / `afl-build-timeout` 告警键。
回路 `harness_afl_build_result.py fail|hang`：旧版失败无告警、卡住留孤儿（双红）；新版均告警且无孤儿（双绿）。
真机未主动触发构建（避免动生产规则），下次 timer 15:15 自然走新路径。

**Y3 ai-bot 每条反馈都 spawn audit.py（放大器，非死锁）**
audit.py 自身有单实例锁，重复实例会很快退出，所以不是"堆积"，但解释器启动成本每条都付。
高频不同域名反馈会放大成 CPU 尖峰，与 DNS 主链路争抢。
→ v2：进程内最小间隔 `AIBOT_AUDIT_GAP`（默认 60s），并在 spawn 异常时接 `aibot-audit-spawn-fail` 告警。
回路 `harness_aibot_audit_spawn.py`：20 条反馈旧版启动 20 次（红），新版 1 次（绿）。

**Y4 面板 shell=True 数据流核实（结论：无可达注入，但抓到登录转义缺陷）**
逐点追完 5 处 shell 执行：
- `svcs` 为硬编码服务名列表；`nic` 为硬编码网卡名；`agh_get(path)` 全部为字面常量；
- `_ss(...)` 的 cmd 为常量；`local_ip_set` 为常量管道；
- 含 `host` 插值的 `dot_query` / `_doq_session` / `_udp53_rtt` **全文件无调用点**（smartdns 时代遗留死代码）。
即：HTTP 入参无法抵达任何 shell 字符串 → 不存在命令注入。
→ 顺手两件：
  (a) 删除 5 个证明无调用的死函数（`_dot_ctx/_median3/dot_query/_doq_session/_udp53_rtt`，-21.9KB），
      同时清掉两处潜在的 `host` 插值点；区间内仍在用的 `_RESP_CACHE` 原样保留（生成器有断言防误删）。
  (b) **真缺陷**：`agh_login()` 把用户名/密码直插单引号 shell 串，密码含单引号即破坏命令 → 面板登录静默失败、
      AGH 统计全空。改为 `json.dumps` 生成载荷 + `shlex.quote` 逐参数转义。
回路 `harness_panel_login_quote.py`（真 shell + 记录 argv 的 curl + 含引号密码）：
旧版 curl 收到 None（红），新版载荷逐字节一致（绿）。

**新核实项（不改）**：13:25 日志里 `[ai-discovered] JSONDecodeError: line 38101`。
写入链路（audit.py、ad-feature-lib、dynamic-ad-discovery）均为 `tmp + os.rename` 原子替换，
读取端已有 try + `{'ok':True,'data':[],'error':'read failed'}` 降级；该报错为历史瞬时事件，
影响仅"新链路候选"一栏短暂空白，不构成持久损坏 → 不额外改造。

**部署与验证（13:50-13:56）**
- 4 文件线上 md5 与本地补丁 **4/4 SAME**；备份 `.bak-20261122-r21` 四份。
- 三服务重启后 active：keepalive / ai-bot / status-server；failed 单元 0。
- 面板走 nginx 443（真实用户路径）：/status 200、/cn-ad 200、
  /status/api/{resp-latency,unbound,protocols,selfheal,daily} 全 200；5 分钟窗口 0 条 traceback/NameError。
- 真机重登：移走 cookie → 等 30s 缓存过期 → 请求 → 新 cookie 225B 生成、AGH 数据 175832→176417，
  证明新的转义登录路径在生产可用。
- health-loop 全链路 **0 项失败**；周体检 last.txt 为空（严重 0 / 警告 0）。
- 资源：avail 2168MB、load 0.14。

**回滚**：`cp -p <文件>.bak-20261122-r21 <文件> && systemctl restart <unit>`
（keepalive / ai-bot / status-server；ad-feature-lib 无需重启，timer 下次读回旧文件即可）。
