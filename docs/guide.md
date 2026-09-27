# Clash Provider Bench 详细说明

本文档说明测试原理、配置、报告口径、长期比较规则、数据库和已知限制。安装和常用命令见项目根目录的 [README](../README.md)。

## 工作方式

Clash Provider Bench 在同一台 macOS、同一时间表和同一测速参数下，顺序评测多个 Clash/Mihomo 服务商。项目不重新实现代理协议或测速内核：

- [`faceair/clash-speedtest`](https://github.com/faceair/clash-speedtest) 负责加载节点以及 TTFB、抖动、丢包、下载和上传测速。
- [Mihomo RESTful API](https://wiki.metacubex.one/en/api/) 用于可选的逐节点出口 IP、地区、ASN、组织和解锁探测。
- 非 Clash YAML 订阅会尝试 `clash.meta` 等成熟客户端 User-Agent，请求服务商直接返回原生 YAML。工具不会自行重新实现 VLESS、AnyTLS 等协议转换。

若服务商始终只提供 URI 列表，可在本机运行 [`subconverter`](https://github.com/tindy2013/subconverter) 后输入生成的 YAML。不要把订阅 token 发送给公共转换站。

安装脚本从两个项目的官方 GitHub `latest` release 选择当前 macOS 架构，并验证 GitHub 提供的 SHA-256 digest。项目初次开发时验证过 `clash-speedtest` v1.8.8 和 Mihomo v1.19.31，但安装脚本不锁死这两个版本。

## 公平性与长期运行

多个 provider 在一次运行中严格顺序测试，避免并发争用带宽。每次运行按 round-robin 轮换 provider 起始顺序，例如：

```text
第 1 次：taishan → cokecloud
第 2 次：cokecloud → taishan
第 3 次：taishan → cokecloud
```

建议固定网络连接、测速端点、文件大小、并发和超时，关闭其他大流量任务，并保持 Mac 供电与散热条件一致。连续运行 3～7 天后再观察晚高峰衰减和 CV。

测速本身会消耗订阅流量。大致流量为“成功节点数 ×（下载 MB + 上传 MB）× 每天次数”，另加协议开销。

## 配置

完整配置示例见 [`examples/bench.toml`](../examples/bench.toml)。

### Provider

订阅 URL 应通过 `.env` 引用：

```toml
[[providers]]
name = "taishan"
source_env = "TAISHAN_SUB_URL"
user_agent = "clash.meta"
```

本地 YAML 使用 `path`，并删除 `source_env`：

```toml
[[providers]]
name = "local-snapshot"
path = "./private/provider.yaml"
```

每个 provider 必须且只能配置 `path` 或 `source_env` 之一。

### 主要参数

| 参数 | 说明 |
|---|---|
| `regions` | 测试地区；空数组表示全部。支持 JP/HK/SG/US/TW/KR/UK/DE，并可覆盖正则。 |
| `engine.speed_mode` | `fast`、`download` 或 `full`。 |
| `engine.server_url` | 所有 provider 共用的测速端点。无 path 时按 `clash-speedtest` 约定使用 `/__down` 和 `/__up`。 |
| `download_size_mb` / `upload_size_mb` | 每个节点的测速大小。 |
| `timeout_seconds` | 节点测速超时。 |
| `concurrent` | 单节点下载并发；公平比较期间应保持固定。 |
| `progress_interval_seconds` | 测速内核没有逐节点事件时，终端进度心跳的间隔秒数。 |
| `providers.user_agent` | 可选的订阅 User-Agent；服务商返回 Base64 URI 列表时可设为 `clash.meta`。 |
| `enrichment.enabled` | 启动隔离 Mihomo，获取出口 IP、国家、地区、ASN 和组织。 |
| `enrichment.unlock` | 是否执行配置的服务可用性检测。 |
| `enrichment.checks` | 只检测指定服务，例如 `["chatgpt"]`；可选值为 ChatGPT、YouTube、Netflix。 |
| `enrichment.chatgpt_unsupported_countries` | 按实际出口国家标记已知不支持地区；建议根据 OpenAI 官方列表维护。 |
| `report.timezone` | 报告展示、文件命名、日间和晚高峰分桶使用的 IANA 时区；示例固定为 `Asia/Shanghai`。 |

### ChatGPT 优先的推荐参数

若主要目的是筛选可用 ChatGPT 节点，推荐使用示例配置中的组合：

```toml
[engine]
speed_mode = "download"
server_url = "https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg"
download_size_mb = 5
timeout_seconds = 15
concurrent = 2
progress_interval_seconds = 5

[enrichment]
enabled = true
timeout_seconds = 15
unlock = true
checks = ["chatgpt"]
chatgpt_unsupported_countries = ["CN", "HK", "MO"]
```

这套参数用 5 MB 下载提供粗粒度线路质量信号，同时避免对几十个节点逐一进行大流量测速；并发 2 和 15 秒超时对远距离线路更宽容。上传大小在 `download` 模式下不会使用。JP、SG、US 适合作为 ChatGPT 常用候选地区；HK 仍可保留在测试列表中，作为识别实际出口和地区限制的对照组。OpenAI 当前支持地区应以其[官方列表](https://help.openai.com/en/articles/7947663-chatgpt-supported-countries)为准。

ChatGPT 检测通过 `curl_cffi` 使用 Chrome TLS/JA3/HTTP2 指纹访问主站及一个无需登录即可返回认证状态的后端端点，并结合实际出口国家和 Cloudflare 响应分类。每个节点使用独立会话，避免节点切换后复用上一个出口的连接或 Cookie：

- `available`：未登录主页请求成功。
- `unsupported-country`：实际出口位于配置的非支持地区，或响应明确表示地区不支持。
- `challenge`：Cloudflare 要求挑战；这类出口可能在浏览器偶尔可用，但 CLI、桌面应用或长连接通常不够稳定。
- `blocked`：明确返回阻断响应。
- `rate-limited` / `http-*` / `error`：限流、异常 HTTP 响应或网络错误。

不能用普通 Python `urllib` 的响应直接判断 ChatGPT：它的 TLS 指纹可能让所有正常出口都收到 `Cf-Mitigated: challenge`，从而产生系统性假阴性。探测客户端及其版本会写入 `comparison_key`，更换客户端后旧结果不会和新结果混合统计。

检测不读取账号、Cookie 或 token，因此 `available` 代表节点具备未登录网络访问条件，不保证账号登录、工作区 IP 白名单或对话请求一定成功。OpenAI 官方也说明 ChatGPT 会使用主站、认证、静态资源和 WebSocket 等多个域名；完整网络要求见其[网络建议](https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps)。

## 报告文件

- `reports/runs/<时间>_<run-id>.md/.html`：每次运行的独立报告，包括 partial/failed 运行。
- `reports/latest.md/.html`：最近一次运行，不混入历史。
- `reports/trend-7d.md/.html`：与趋势锚点条件一致的完整历史。
- `reports/results.csv`：测量和运行元数据的 UTF-8 CSV。
- `data/bench.sqlite3`：完整历史数据库。

报告同时按以下维度展示：

- provider × 地区
- provider × 地区 × 协议，例如 AnyTLS、Hysteria2、VLESS、Shadowsocks、Trojan、TUIC
- provider × 地区的 ChatGPT 可用率，以及单次运行的逐节点 ChatGPT 结果

## 统计口径

`clash-speedtest` 的延迟是 6 次 HTTP HEAD 的平均 TTFB，抖动是这些样本的标准差，丢包是失败样本比例。SQLite 保存的是这一组原始聚合值；报告中的 P50/P95 是同一分组、指定时间窗口内节点测量记录的分布，不是未暴露的 6 个底层样本。

下载和上传统一保存为十进制 Mbps。原 CLI 的 KiB/MiB 单位先换算为 bytes/s，再乘以 8。

- `available`：延迟成功且丢包低于 100%。
- `status=ok`：节点可用，并且当前模式要求的吞吐阶段没有错误。
- 延迟可用但下载失败：计入可用节点，同时计入测速失败。
- 测速成功率和失败率的分母都是总节点测量数。

每个指标后的 `n` 是实际使用的有效样本数：

- TTFB、抖动、下载和上传只使用测速可用且对应数值非空的记录。
- 丢包使用所有具有丢包数值的记录，包括 100% 丢包的失败节点。
- 下载 CV = 下载速度总体标准差 / 均值，至少 2 个下载样本才计算；越低越稳定。
- 晚高峰按 `report.timezone` 计算，为 20:00–23:59；日间基准为 09:00 和 14:00。示例固定使用北京时间 `Asia/Shanghai`，不受运行机器系统时区影响。报告显示两个时段各自的有效下载样本数；负衰减表示晚高峰反而更快。

## 哪些历史数据可以比较

每次运行都会生成不含秘密的 `comparison_key`。下列条件变化时会建立新的条件组，旧数据仍保留但不会混入新趋势：

- provider 集合
- `clash-speedtest` 版本
- 测速模式、端点以及端点查询参数哈希
- 下载/上传大小、并发和超时
- 地区集合
- enrichment/unlock 开关
- macOS 版本和 CPU 架构
- 默认网络接口
- 系统代理启用类型和代理服务器哈希
- 活动 TUN/VPN 接口

趋势只纳入相同 `comparison_key` 且状态为 `ok` 的运行。某个 provider 整体失败会让该次运行成为 `partial` 或 `failed`：数据和单次报告仍会保留，但不会进入公平趋势。

升级前的旧数据按原有 `config_digest + engine` 形成带 `legacy:` 前缀的独立组。旧数据仍可查看和分析，但因为当时未记录完整测试条件，不能与新条件直接比较。

## 测速失败与附加信息失败

两类状态彼此独立：

- `status/error`：节点测速状态和错误。
- `enrichment_status/enrichment_error`：出口 IP、ASN、组织和解锁探测状态。

Mihomo enrichment 无法启动或出口查询失败时，已经完成的延迟、丢包和吞吐结果仍然保存，不会改变节点可用率或测速成功率。

## 基础设施多样性

报告使用出口信息计算：

- 出口信息覆盖率
- 独立出口 IP 数
- 独立 ASN 数
- 独立组织数
- 最大出口集中度
- 最大 ASN 集中度
- enrichment 异常数量

每个稳定节点只取时间窗口内最新一次出口观测，避免一天重复测试多次就把同一个出口误算成多份资源。出口覆盖不足时，这些数字只代表已观测样本，不代表服务商完整库存。

## 网络环境记录

每次运行记录：

- macOS 版本与 CPU 架构
- 默认网络接口
- HTTP、HTTPS、SOCKS、PAC 是否启用
- 代理服务器的不可逆短哈希
- 检测到的活动 TUN/VPN 接口
- provider 实际执行顺序

SQLite 中的原始时间继续使用带时区的 UTC，保证历史数据和跨机器处理一致；报告展示、趋势时段划分以及报告文件名转换为配置的 `Asia/Shanghai`。终端运行进度同样使用该时区，并在每行开头输出带 `+08:00` 偏移的时间戳，例如：

```text
[2026-09-27 20:15:06+08:00] [1/2 taishan] 附加检测 8/57 完成：available:US
```

普通系统代理开启不会让测试强制终止。工具不会保存 Wi-Fi SSID、本机 IP、代理服务器明文或代理凭据，也无法识别运营商临时路由变化。

## SQLite 结构与迁移

- `runs`：运行状态、对比条件、公开测速参数、内核版本、provider 顺序和脱敏网络环境。
- `provider_runs`：每个 provider 的执行顺序、开始/结束时间、状态、测量数和脱敏错误。
- `measurements`：节点、地区、协议、测速指标与状态，以及独立的 enrichment 状态和出口信息。

启动时会自动增量迁移旧数据库，只增加列和表，不删除或重写历史记录。升级前仍建议备份：

```bash
sqlite3 data/bench.sqlite3 ".backup 'data/bench.backup.sqlite3'"
```

节点稳定键以 provider、服务器、端口、协议和名称生成 SHA-256 截断值。服务商改名或更换服务器时会被视为新节点；仅含远端 `proxy-providers:` 的配置可能退化为 provider + 节点名。

## 定时任务说明

`launchd` 保存的是安装日程时使用的 Python 解释器，因此应先激活项目 `.venv`。Mac 睡眠时错过的固定时刻通常不会补跑；需要时可唤醒后手动补跑。

中途修改测速参数或升级测速内核会开始新的趋势条件组，这是预期行为。

## 安全与限制

- 订阅 URL 只从环境变量读取，临时 YAML 权限为 `0600`，运行结束后删除。
- URL/token 不写入日志、数据库或报告。
- 顶层 `proxies:` 能生成最稳定的节点键；远端 `proxy-providers:` 可能缺少服务器字段。
- `clash-speedtest` TSV 不暴露 6 次 HEAD 的逐次样本，也不单独暴露内部 download/upload error 字段。
- Mihomo `GLOBAL` 选择依赖 global 模式；复杂 rule-provider 配置可能无法 enrichment，但测速结果仍会保留。
- 解锁检测只代表匿名 HTTP 可达性，可能受页面策略和登录账户区域影响。

## 开发与验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
clashbench run --config examples/bench.toml --mock
```

Homebrew Python 3.13 可能忽略 setuptools 生成的隐藏 `__editable__` `.pth` 文件，建议使用普通 `python -m pip install .`，不要依赖 `pip install -e .`。
