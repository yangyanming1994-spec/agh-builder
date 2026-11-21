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
