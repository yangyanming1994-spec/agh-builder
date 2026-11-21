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
