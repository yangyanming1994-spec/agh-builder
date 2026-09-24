# AGH-Builder

**专为 AdGuard Home 打造的 DNS 规则合并工具。**

> ⚠️ **重要声明：本工具仅适用于 AdGuard Home。**
> 不支持 Clash、V2Ray、Shadowrocket、Quantumult X、SingBox、dnsmasq 等其他程序。

## 一键订阅（推荐）

规则每 2 小时自动更新，直接复制以下链接添加到 AdGuard Home 即可：

**拦截规则：**
```
https://raw.githubusercontent.com/yangyanming1994-spec/agh-builder/main/adguard-home-rules.txt
```

**白名单：**
```
https://raw.githubusercontent.com/yangyanming1994-spec/agh-builder/main/adguard-home-allowlist.txt
```

添加方法：AdGuard Home → 过滤 → DNS 封锁列表 → 添加列表 → 粘贴上面的链接。

## 这是什么

AGH-Builder 从多个开源广告拦截规则源拉取规则，自动清洗、去重、合并，产出 AdGuard Home 能直接订阅的 DNS 层拦截规则。

## 为什么需要它

AdGuard Home 工作在 DNS 层，只能看到域名，看不到 HTTP 内容。很多规则源包含：

- 元素隐藏规则（`##`、`#@#`）— DNS 层无效
- JS 注入规则（`#%#`）— DNS 层无效
- HTTP 修饰符（`$script`、`$image`、`$xhr`…）— DNS 层无此概念
- 带路径的规则（`||domain/path`）— DNS 查询只有域名
- 纯 IP 地址 — 不适用域名拦截

本工具自动剔除这些无效规则，只保留 AdGuard Home DNS 层能 100% 生效的条目。

## 功能

- ✅ 多源并行下载，自动重试
- ✅ 自动剔除元素隐藏、JS 注入、HTTP 修饰符等 DNS 层无效规则
- ✅ 白名单冲突检测：白名单域名自动从黑名单中剔除（含子域名严格匹配）
- ✅ hosts 格式自动归一化为 `||domain^`
- ✅ 支持正则规则（可关闭）
- ✅ 输出统计 JSON

## 用法

```bash
python3 build.py                 # 默认使用 sources.txt
python3 build.py --no-regex      # 不保留正则规则
python3 build.py --no-hosts      # 不生成 hosts 格式
python3 build.py --no-strict     # 白名单仅精确匹配（默认含子域名）
```

## 配置

- `sources.txt` — 黑名单规则源（每行一个 URL 或 `file:` 本地文件）
- `allow-sources.txt` — 白名单规则源
- `local/` — 本地自定义规则

## 输出（dist/ 目录）

| 文件 | 说明 |
|------|------|
| `adguard-home-rules.txt` | AGH 拦截规则，可直接作为 DNS 拦截列表订阅 |
| `adguard-home-allowlist.txt` | AGH 白名单 |
| `hosts.txt` | hosts 格式 |
| `stats.json` | 统计信息 |

## 部署到 AdGuard Home

1. 运行 `python3 build.py`
2. 将 `dist/adguard-home-rules.txt` 上传到你的服务器或托管在 GitHub
3. 在 AdGuard Home → 过滤 → DNS 封锁列表中添加订阅地址
4. 白名单加入 `dist/adguard-home-allowlist.txt`

## 环境要求

- Python 3.6+
- 无第三方依赖，纯标准库

## License

MIT
