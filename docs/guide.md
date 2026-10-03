# Clash Provider Bench 详细说明

本文档说明程序怎么测试节点、参数怎么设置、报告里的数字怎么算，以及连续测试时该怎样比较结果。安装步骤和常用命令见 [README](../README.md)。

## 工作方式

Clash Provider Bench 在同一台 Mac 上，按同一套参数依次测试多个 Clash/Mihomo 服务商。程序调用现有工具完成测速和代理连接：

- [`faceair/clash-speedtest`](https://github.com/faceair/clash-speedtest) 加载节点，测试 TTFB（收到首个响应字节前的等待时间）、抖动、丢包、下载和上传速度。
- 程序通过 [Mihomo RESTful API](https://wiki.metacubex.one/en/api/) 切换节点，再查询出口 IP、地区、ASN（自治系统编号）和所属组织，并检查能否访问指定服务。
- 如果订阅没有返回 Clash YAML，程序会尝试 `clash.meta` 等 User-Agent（HTTP 请求中标明客户端的字段），请服务商返回 YAML。程序本身不把 VLESS、AnyTLS 等节点链接转换成配置。

如果服务商只提供节点链接列表，可以在本机运行 [`subconverter`](https://github.com/tindy2013/subconverter)，再让本工具读取转换后的 YAML。订阅链接包含 token（访问令牌），不要把它交给公共转换站。

安装脚本从两个项目的 GitHub 最新版本中下载适合当前 Mac 的程序，并用 GitHub 提供的 SHA-256 校验值检查下载文件。项目初次开发时测试过 `clash-speedtest` v1.8.8 和 Mihomo v1.19.31；安装脚本会下载最新版本，并没有固定使用这两个版本。

## 怎样让连续测试更容易比较

每次运行都先测完一家服务商，再测下一家，避免两家同时下载、互相占用带宽。在测试条件相同的连续运行中，程序会轮流更换先测的服务商，例如：

```text
第 1 次：taishan → cokecloud
第 2 次：cokecloud → taishan
第 3 次：taishan → cokecloud
```

连续测试时，尽量使用同一种网络连接、同一个测速地址，并固定文件大小、并发数和超时时间。测试期间关闭其他大量下载或上传的任务，也尽量保持 Mac 的供电和散热条件一致。连续运行 3～7 天后，可以比较白天和晚间的速度，以及下载速度的 CV（变异系数，用来描述速度波动）。

测速会消耗订阅流量。可以用“成功节点数 × 每个节点的下载和上传量 × 每天测试次数”估算主要流量；失败前已经传输的数据和代理协议本身也会消耗一些流量。

## 配置

完整配置示例见 [`examples/bench.toml`](../examples/bench.toml)。

### 添加服务商

把订阅链接写在 `.env` 中，配置文件只填写环境变量名：

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

每个 `[[providers]]` 对应一家服务商，`name` 不能重复。读取本地文件时填写 `path`，读取订阅链接时填写 `source_env`，两者只选一个。

### 主要参数

| 参数 | 说明 |
|---|---|
| `regions` | 只测试这些地区的节点，空数组表示全部。支持 JP/HK/SG/US/TW/KR/UK/DE，也可以在 `region_patterns` 中自定义识别节点名的正则表达式。 |
| `engine.speed_mode` | `fast` 只测延迟、抖动和丢包；`download` 加测下载；`full` 再加测上传。 |
| `engine.server_url` | 所有服务商共用的测速地址。URL 没有路径时，`clash-speedtest` 会使用 `/__down` 和 `/__up`。 |
| `download_size_mb` / `upload_size_mb` | 每个节点下载和上传多少数据。 |
| `two_stage_download_size_mb` / `two_stage_upload_size_mb` | 两阶段模式中，通过 ChatGPT 基础检查的节点下载和上传多少数据。实际执行哪项取决于 `speed_mode`。 |
| `timeout_seconds` | 节点测速时等待响应的超时时间，单位为秒。 |
| `concurrent` | 测试一个节点时同时发起的下载请求数；比较服务商时应保持一致。 |
| `progress_interval_seconds` | 测速程序暂时没有返回节点结果时，每隔多少秒打印一次已运行时长。 |
| `providers.user_agent` | 请求订阅时使用的 User-Agent。服务商返回 Base64 编码的节点链接列表时，可以尝试设为 `clash.meta`。 |
| `enrichment.enabled` | 是否另行启动 Mihomo，查询出口 IP、国家、地区、ASN 和所属组织。 |
| `enrichment.unlock` | 是否检查节点能否访问指定服务。 |
| `enrichment.checks` | 要检查的服务，例如 `["chatgpt"]`；可填写 `chatgpt`、`youtube`、`netflix`。 |
| `enrichment.workers` | 同时检查多少组节点，范围 1～16。每组由一个 worker（工作线程）检查，并使用独立的 Mihomo 进程，避免切换节点时互相影响。 |
| `enrichment.chatgpt_unsupported_countries` | 出口国家在这个列表中时，程序将 ChatGPT 结果记为地区不支持。可按 OpenAI 官方列表调整。 |
| `enrichment.websocket_hold_seconds` | 两阶段模式中，要求 Realtime API WebSocket 保持连接多少秒，范围 5～120。 |
| `enrichment.websocket_reconnect_attempts` | WebSocket 握手成功后，如果持续连接检查失败，最多重新连接多少次，范围 0～3。 |
| `enrichment.openai_api_key_env` | 从哪个环境变量读取 OpenAI API key。程序只在内存中使用这个值，不保存到文件。 |
| `enrichment.realtime_model` | 连接 Realtime API 时请求的模型，示例为 `gpt-realtime-2.1`。 |
| `report.timezone` | 显示时间、命名报告文件，以及划分白天和晚高峰时使用的时区。示例使用北京时间 `Asia/Shanghai`。 |
| `notification.enabled` | 每次实际测试结束后，是否发送 macOS 通知。 |
| `notification.mode` | 当前只支持 `macos`。通知会告诉你总共有多少节点通过 ChatGPT 基础检查，以及各家服务商分别有多少个。 |

### 主要想找能访问 ChatGPT 的节点时怎么设置

如果主要想找能访问 ChatGPT 的节点，可以使用下面这组示例参数：

```toml
[engine]
speed_mode = "download"
server_url = "https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg"
download_size_mb = 5
two_stage_download_size_mb = 50
two_stage_upload_size_mb = 20
timeout_seconds = 15
concurrent = 2
progress_interval_seconds = 5

[enrichment]
enabled = true
timeout_seconds = 15
workers = 4
websocket_hold_seconds = 15
websocket_reconnect_attempts = 1
openai_api_key_env = "OPENAI_API_KEY"
realtime_model = "gpt-realtime-2.1"
unlock = true
checks = ["chatgpt"]
chatgpt_unsupported_countries = ["CN", "HK", "MO"]
```

运行两阶段测试：

```bash
clashbench run --config examples/bench.toml --two-stage
```

程序先用 `fast` 模式测试所有选中地区的节点，记录延迟、抖动和丢包，再查询出口信息，检查 ChatGPT 首页、后端接口、登录服务和静态资源服务器能否响应。

只有 ChatGPT 基础结果为 `available` 的节点才会进入第二阶段。程序会连接 OpenAI Realtime API，检查 WebSocket 连接，并按 `speed_mode` 测下载或上传。上面的例子使用 `download`，因此只测下载，每个通过检查的节点下载 50 MB；改成 `full` 后，还会上传 20 MB。

报告保留所有节点在第一阶段测得的延迟、抖动、丢包和可用状态，第二阶段补上下载、上传结果，以及测速是否成功。这样，即使第二阶段下载失败，也能看出该节点曾通过基础网络检查。WebSocket 检查失败后，程序仍会继续测速。没有通过 ChatGPT 基础检查的节点会跳过下载和上传，也不会计为下载/上传失败。

多个 worker 各自运行独立的 Mihomo，使用自己的节点选择器、连接和 Cookie。示例将 JP、SG、US 作为主要测试地区，保留 HK 用来观察地区限制的结果；是否支持某个地区应以 OpenAI 的[官方列表](https://help.openai.com/en/articles/7947663-chatgpt-supported-countries)为准。

如果只想尽快检查哪些节点能访问 ChatGPT，可以运行：

```bash
clashbench run --config examples/bench.toml --quick
```

`--quick` 会让本次运行使用 `fast` 模式，检查延迟、抖动、丢包、出口和 ChatGPT，跳过下载和上传，也不执行第二阶段的 WebSocket 检查。普通 `run` 会按配置测试所有选中节点；`--two-stage` 则先检查 ChatGPT，再对通过的节点测速。这三种运行方式会分别生成对比条件 ID，趋势报告会分别统计。

程序使用 `curl_cffi`，让 TLS 和 HTTP/2 请求的特征尽量接近 Chrome，再根据实际出口国家和服务器响应判断结果。每换一个节点，程序都会新建会话，避免沿用上一个节点的连接或 Cookie。程序检查这些地址：

- `chatgpt.com` 首页和 `/backend-api/me`：检查能否打开首页，以及 ChatGPT 后端能否返回响应。后端要求登录时，也会进一步检查是否明确遭到阻止。
- `auth.openai.com`：检查能否连接登录服务并收到 HTTP 响应。401/403 等响应说明服务器已经答复请求，但不表示账号登录成功。
- `cdn.oaistatic.com`：检查能否连接静态资源服务器。根地址返回 403/404 也说明已经收到服务器响应。

报告会用以下状态表示 ChatGPT 基础检查的结果：

- `available`：首页、后端、登录服务和静态资源服务器都通过检查。程序会继续测试这个节点，但此时还没有验证它能否保持长连接。
- `unsupported-country`：实际出口国家在配置的排除列表中，或者服务器明确提示该地区不支持。
- `challenge`：Cloudflare 要求完成验证。浏览器可能允许你手动验证，但本工具无法据此确认后续请求是否正常。
- `blocked`：服务器拒绝请求，或明确提示该出口被阻止。
- `rate-limited` / `http-*` / `error`：分别表示请求被限流、服务器返回其他异常状态码，以及发生网络或请求错误。

项目尚未验证 ChatGPT 网页实际使用的 WebSocket 接口，因此第二阶段使用 OpenAI 公开的 `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`。这个地址属于 Realtime API，测试结果说明节点与该 API 之间的连接情况。ChatGPT 网页使用的接口不同，使用报告时需要结合基础检查结果判断。

配置 API key 后，程序先完成 WebSocket 握手，再等待服务器发送 `session.created`，确认会话已经建立。程序还会发送 Ping，并等待对应的 Pong 回复。收到会话事件、Ping 得到回复，而且连接保持到 `websocket_hold_seconds` 指定的时间，才会记为稳定。握手成功后的检查失败时，程序按配置的次数重新连接同一地址。

报告中的状态含义如下：

- `api-auth-boundary`：没有配置 API key，服务器返回 101 或 401/403。程序记录服务器已响应，然后结束连接；没有测试持续连接，因此不记录持续时长或异常断开，也不计入稳定率。
- `handshake-failed`：连接或 WebSocket 握手失败，持续时长为 0。
- `stable`：第一次连接就建立了会话，收到 Pong，并保持到设定时间。
- `stable-after-reconnect`：第一次连接未通过检查，重连后建立了会话，收到 Pong，并保持到设定时间。
- `unstable-disconnected`：握手成功过，但连接提前关闭，后续尝试也没有恢复稳定连接。
- `unstable-unresponsive`：连接没有立即关闭，但发送 Ping 后迟迟没有收到对应的 Pong。连接可能已经失效，也可能是中间网络停止传输。
- `session-failed`：握手成功后没有收到 `session.created`，或者服务器返回 `error` 事件、无法解析的协议数据。

数据库中的 `chatgpt_websocket` 记录第一次握手的结果；`chatgpt_websocket_seconds` 记录各次尝试中保持连接最久的那一次，不把多次短连接相加。`chatgpt_websocket_disconnects` 记录提前关闭或网络断开的次数，`chatgpt_websocket_reconnect` 记录重连结果。看到 `upgrade-101` 时，还要查看会话和稳定性结果。

普通 Python `urllib` 发出的请求与浏览器有不同的 TLS 特征，可能让本来能访问 ChatGPT 的节点也收到 `Cf-Mitigated: challenge`。这也是本工具使用 `curl_cffi` 的原因。程序会根据请求库、检查地址、保持时间、重连次数和验证方法生成 `comparison_key`；改动这些条件后，趋势报告会分别统计新旧结果。

程序检查 ChatGPT 时不读取你的账号或浏览器 Cookie。要检查 Realtime API 会话能否保持连接、断开后能否重连，需要在 `examples/.env` 中填写 `OPENAI_API_KEY`，也可以用 `openai_api_key_env` 指定其他环境变量名。程序只在内存中使用 key，不把它写入日志、数据库或报告。没有 key 时，程序只检查服务器是否响应。

第一阶段的 `available` 说明未登录时的基础请求通过检查，第二阶段的 `stable` 说明与 Realtime API 的连接通过检查。账号权限和真实聊天请求仍需要在 ChatGPT 中验证。ChatGPT 会访问多个域名，具体网络要求见 OpenAI 的[网络建议](https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps)。

## 报告文件

- `reports/runs/<时间>_<run-id>.md/.html`：每次运行单独保存一份报告，部分服务商失败或整次失败时也会保存。
- `reports/latest.md/.html`：最近一次运行的结果。
- `reports/trend-7d.md/.html`：最近 7 天中，与所选运行条件相同且所有服务商都完成测试的结果。默认按最近一次运行的条件选择数据。
- `reports/results.csv`：导出的测量记录、运行参数和网络环境，文件编码为 UTF-8。
- `data/bench.sqlite3`：完整历史数据库。

报告会按服务商和地区比较网络指标，还会进一步按协议分组，例如 AnyTLS、Hysteria2、VLESS、Shadowsocks、Trojan 和 TUIC。

ChatGPT 部分展示各家服务商在每个地区有多少节点通过基础检查，单次报告还会列出每个节点的结果。WebSocket 部分展示握手成功数、最终稳定率、持续连接时长、提前断开次数和重连结果。

## 报告里的数字怎么算

`clash-speedtest` 对每个节点发送 6 次 HTTP HEAD 请求。它用平均 TTFB 表示延迟，用这些请求的标准差表示抖动，用失败请求的比例表示“丢包”。这里的丢包是 HTTP 请求失败率，不是 ICMP ping 的丢包率。

SQLite 保存测速工具返回的每个节点的结果。报告再按服务商、地区和时间范围，计算这些记录的 P50（中位数）和 P95（95% 的样本不超过的值）。因此，报告统计的是节点测量记录；测速工具没有提供那 6 次请求各自的原始结果。

下载和上传速度统一保存为 Mbps（每秒百万比特）。程序先把测速工具返回的字节速度按 1024 进制换算成 bytes/s（每秒字节数），再乘以 8、除以 1,000,000。

- 每测一个节点，记为一条测量记录。同一节点测多次，就有多条记录。
- 节点可用率 = 延迟测试成功且丢包低于 100% 的记录数 / 全部记录数。两阶段模式始终使用第一阶段的基础网络结果。
- 下载/上传成功率的分母，是实际尝试下载或上传的记录数。报告把 `status=ok` 且有下载速度的记录计为成功，其余已尝试记录计为失败。快速模式和两阶段中未通过 ChatGPT 检查的节点不计入这个分母。
- 延迟检查通过但下载失败时，该记录仍计为节点可用，同时计为下载/上传失败。
- 旧记录没有保存是否尝试下载/上传的标记，报告按已经尝试处理。

每个指标后的 `n` 告诉你这个数字使用了多少条记录：

- TTFB、抖动、下载和上传只使用节点可用、且对应数值存在的记录。
- 丢包使用所有带有丢包数值的记录，包括 100% 丢包的失败节点。
- 下载 CV = 下载速度的总体标准差 / 平均值 × 100%。至少有 2 条下载记录才计算，数值越低说明这组速度越接近。分组里可能包含不同节点，不能仅凭这个数字判断某一个节点是否稳定。
- 晚高峰按 `report.timezone` 划分，为 20:00–23:59；白天的比较数据取 09:00–09:59 和 14:00–14:59。示例使用北京时间 `Asia/Shanghai`。报告用两个时段的下载中位数计算速度下降比例，并显示各用了多少条记录；比例为负时，表示晚间中位数反而更高。

## 哪些历史数据可以比较

程序根据每次运行的参数生成 `comparison_key`（对比条件 ID），其中不包含订阅或 API key。下列条件发生变化后，程序会生成新的 ID，趋势报告会分别统计：

- 测试了哪些服务商
- `clash-speedtest` 版本
- 测速模式、测速地址，以及 URL 查询参数的哈希值
- 下载/上传大小、并发和超时
- 测试了哪些地区
- 是否查询出口信息、是否检查指定服务，以及检查了哪些服务
- 同时查询出口和检查服务的 worker 数
- ChatGPT 请求库的版本和请求特征，以及配置的排除地区
- Realtime API 地址、模型、保持时长、重连次数、验证方法和是否配置 API key（不包含 key 的值）
- 两阶段结果如何合并，以及用于显示时间和划分时段的时区
- macOS 版本和 CPU 架构
- 默认网络接口
- 启用了哪些系统代理，以及代理服务器地址的哈希值
- 活动 TUN/VPN 接口

趋势报告只统计 `comparison_key` 相同、运行状态为 `ok` 的数据。这里的 `ok` 表示各家服务商都完成了测试，其中仍可能有失败节点，这些失败记录也会参与统计。

如果某家服务商整体测试失败，该次运行会记为 `partial`（部分完成）或 `failed`（失败）。已取得的数据和单次报告仍会保存，但该次运行不会进入趋势统计。

升级前的记录没有保存完整测试条件，报告会按原来的 `config_digest + engine` 把它们分组，组名以 `legacy:` 开头。你仍然可以查看这些记录并生成报告，它们会与新记录分别统计。

## 测速失败与附加信息失败

数据库分别记录测速结果和额外检查结果：

- `status/error`：节点测速是否成功，以及发生了什么错误。
- `enrichment_status/enrichment_error`：查询出口 IP、ASN、组织，或检查服务和 WebSocket 时是否出错。

如果额外检查用的 Mihomo 没有启动成功，或者出口查询失败，程序仍会保存已经测得的延迟、丢包和下载/上传结果。额外检查失败不会改变节点可用率或下载/上传成功率。

## 服务商的节点是否共用出口和网络

节点数量多，不一定代表有很多不同的出口。报告会查询出口信息，展示：

- 有多少可用节点查到了出口 IP，以及这些节点占全部可用节点的比例。
- 查到了多少个不同的出口 IP、ASN 和所属组织。
- 共用最多的那个出口 IP 占已查到 IP 的节点多少比例。
- 共用最多的那个 ASN 占已查到 ASN 的节点多少比例。
- 有多少条记录在额外检查时出错。

每个节点只取统计时间范围内最新的一条测量记录，这样一天测试多次也不会把同一个节点重复计数。如果很多节点没有查到出口信息，这些数字只能说明已经查到的部分，不能代表服务商的全部资源。

## 网络环境记录

程序每次运行都会记录这些信息，方便你回头判断网络环境是否影响了结果：

- macOS 版本与 CPU 架构
- 默认网络接口
- HTTP、HTTPS、SOCKS、PAC 是否启用
- 代理服务器地址的短哈希值，用来辨别是否换了代理服务器
- 检测到的活动 TUN/VPN 接口
- 各家服务商实际测试的先后顺序

SQLite 使用 UTC（协调世界时）保存原始时间，方便比较不同机器上的记录。报告、文件名和终端进度使用 `report.timezone` 设置的时区。示例设置为北京时间 `Asia/Shanghai`，终端每行开头会显示时间和 `+08:00` 时区偏移，例如：

```text
[2026-09-27 20:15:06+08:00] [1/2 taishan] 附加检测 8/57 完成：available:US
```

开启普通系统代理后，程序仍会继续测试。它不会保存 Wi-Fi 名称、本机 IP、代理服务器的完整地址或登录凭据。记录的环境信息也无法说明运营商是否临时更改了路由。

## 数据库保存什么，升级时怎么处理

- `runs`：每次运行的状态、测试参数、工具版本、服务商顺序和网络环境。
- `provider_runs`：每家服务商的开始和结束时间、测试顺序、状态、测量数和错误信息。
- `measurements`：每个节点的地区、协议、测速结果，以及出口信息、服务检查和 WebSocket 检查结果。

程序启动时会给旧数据库补上新表和新字段，保留已有记录。升级前可以先备份：

```bash
sqlite3 data/bench.sqlite3 ".backup 'data/bench.backup.sqlite3'"
```

程序根据服务商名称、节点服务器、端口、协议和节点名称生成 `node_key`（节点标识），用于查找同一节点的历史。服务商名称、节点名称或服务器改变后，程序会把它视为新节点。如果配置只含远端 `proxy-providers:`、没有提供服务器等信息，节点标识可能只能依据服务商名称和节点名称生成。

## 定时任务说明

安装定时任务前，先激活项目的 `.venv`，因为 `launchd` 会保存安装时使用的 Python 路径。只要用户仍然登录、Mac 没有睡眠，锁屏后任务也能按时启动。任务开始时会运行 `caffeinate -i`，让 Mac 在测试完成前保持唤醒，避免因空闲而睡眠。

定时任务默认使用 `--two-stage`：先检查 ChatGPT，再对通过的节点测下载或上传，结束后按通知配置发送结果。可以用 `--regions JP,SG,US`，跳过示例中已设为不支持的 HK。如果要按配置测试全部选中节点，安装时加上 `--full`；如果只想检查能否连接，不需要下载或上传速度，加上 `--quick-only`。重新安装会替换现有的时间安排。

`launchd` 的 `StartCalendarInterval` 不会主动唤醒已经睡眠的 Mac。Mac 下次醒来时，macOS 会补跑一次；如果睡眠期间错过了多个测试时间，也只补跑一次。如果需要整点唤醒 Mac，需要另外设置系统唤醒时间。

示例配置会在每次实际测试结束后发送 macOS 通知，告诉你总共有多少节点通过 ChatGPT 基础检查，以及各家服务商分别有多少个。可以先手动试发一条：

```bash
clashbench notify --config examples/bench.toml --test
```

锁屏时，通知会进入 macOS 通知中心。是否显示在锁定屏幕上，由“系统设置 → 通知”中的设置决定。`run --mock` 不发送通知。如果通知没发出去，程序会在日志中记下错误，测试结果仍然保留。

连续测试期间如果修改参数或升级测速工具，程序会按新的条件另行统计趋势。

## 订阅信息怎么保存，结果有哪些限制

- 订阅链接从环境变量读取。下载后的临时 YAML 文件权限为 `0600`，只有当前用户可以读写，运行结束后删除。
- 程序不会把订阅链接或 token 写入日志、数据库和报告。
- 配置中的顶层 `proxies:` 通常包含完整节点信息，便于追踪同一节点；远端 `proxy-providers:` 可能没有提供服务器等字段。
- `clash-speedtest` 返回的 TSV 没有每次 HEAD 请求的结果，也没有分别提供下载和上传的内部错误字段。
- 额外检查通过 Mihomo 的 `global` 模式切换 `GLOBAL` 节点。复杂配置可能导致检查失败，已经取得的测速结果仍会保存。
- 检查服务时，程序只发送未登录的 HTTP 请求。页面规则和账号所属地区可能影响你实际使用时的结果。
- Realtime API WebSocket 结果可以帮助观察连接问题；ChatGPT 网页的实际聊天仍需要另行验证。

## 开发与验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
clashbench run --config examples/bench.toml --mock
```

在本项目的 Homebrew Python 3.13 环境中，曾遇到 `pip install -e .` 生成的隐藏 `__editable__` `.pth` 文件未被加载。遇到安装后无法导入模块的问题时，可以改用 `python -m pip install .`。
