# Clash Provider Bench

在同一台 macOS、同一时间表、同一测速端点和同一参数下，顺序评测多个 Clash/Mihomo 订阅服务商。项目本身不重写代理协议或测速内核：吞吐/TTFB/抖动/丢包由 `faceair/clash-speedtest` 完成；可选的出口 IP、国家/地区、ASN 和解锁探测由隔离运行的 Mihomo 内核及官方 REST API 完成。

## 已实现

- 输入多个订阅 URL（仅从环境变量读取）或本地 Clash/Mihomo YAML，并为每个来源保留 provider 名称。
- 遇到服务商按 User-Agent 返回 Base64 URI 列表时，自动改用 `clash.meta` 等兼容 UA 请求原生 YAML；不自行重写 VLESS/AnyTLS 等协议转换。
- JP/HK/SG/US/TW/KR/UK/DE 自动归类，可覆盖正则；`--regions JP,HK` 只测指定地区。
- provider 严格顺序执行，节点使用相同 URL、文件大小、并发数和超时；每次运行按 round-robin 轮换 provider 起始顺序，减少固定先后造成的时间偏差。
- 原始结果保存在 SQLite；同时导出 UTF-8 CSV、Markdown 和静态 HTML。
- 指标：可用率、平均 HTTP/TTFB 延迟、跨节点/跨时间 P50/P95、抖动、丢包、上下行 Mbps、失败率。
- 可选 Mihomo enrichment：逐节点出口 IP、国家、地区、ASN/组织；可选 ChatGPT/YouTube/Netflix 最佳努力可达性探测。
- 每次运行生成独立报告；另有最近 3～7 天趋势报告。趋势只混合同一“对比条件 ID”且完整成功的运行，partial/failed 和参数不同的运行不会污染统计。
- 报告同时按 provider × 地区和 provider × 地区 × 协议（AnyTLS、Hysteria2、VLESS、Shadowsocks 等）展示，并为每项 P50/P95/CV 标明实际有效样本数。
- 基础设施观察包括出口覆盖率、独立出口、独立 ASN/组织以及最大出口/ASN 集中度，不把重复定时观测误当作更多资源。
- 记录默认网络接口、系统代理启用状态、活动 TUN/VPN 接口、系统/架构；普通系统代理不会导致强制终止。
- 一次性运行，或生成/安装 macOS `launchd` 日程（默认 09:00、14:00、20:00、22:00、00:00）。
- 每次订阅被下载到运行专属、权限 `0600` 的临时目录，测试结束删除；URL/token 不写日志、数据库或报告。

## 为什么复用这些组件

- [`faceair/clash-speedtest`](https://github.com/faceair/clash-speedtest) 已直接嵌入 Clash/Mihomo 代理适配层，能加载本地配置或订阅，内建 6 次 HEAD 延迟样本、平均延迟、标准差抖动、丢包和上下行测速；非交互输出为 TSV，适合稳定解析。它以 GPL-3.0 发布，本项目只调用外部二进制，不复制其代码。
- [Mihomo RESTful API](https://wiki.metacubex.one/en/api/) 已提供代理列表、节点选择、delay、provider healthcheck 和连接统计。本项目只在 enrichment 阶段启动隔离本地内核，通过 `GLOBAL` selector 逐节点选择。
- [`tindy2013/subconverter`](https://github.com/tindy2013/subconverter) 能转换很多旧订阅格式，但本项目默认不依赖公共转换服务，避免把 token 交给第三方。若输入不是 Clash YAML，建议自行本地运行 subconverter 后再传入文件。

调研时核对的 `clash-speedtest` 版本为 v1.8.8（2026-05-17 发布），Mihomo 为 v1.19.31；安装脚本每次从官方 GitHub `latest` release 动态选择当前 macOS 架构，并验证 GitHub 提供的 SHA-256 digest，因此不把版本写死。

## 安装（macOS）

要求 Python 3.11+。Apple Silicon 和 Intel Mac 均支持。

```bash
cd /path/to/clash-provider-bench
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/install_tools.py
cp examples/.env.example examples/.env
chmod 600 examples/.env
```

编辑 `examples/.env`，填入真实订阅 URL。不要把它提交到 Git。若只用本地 YAML，可在 `examples/bench.toml` 中把 provider 改成 `path = "./private/provider.yaml"`，并删去对应 `source_env`。

安装脚本把官方二进制放在 `tools/bin/`：

- `clash-speedtest`：必需，负责测速。
- `mihomo`：只有 `[enrichment] enabled = true` 时使用。

## 快速开始

先检查配置与二进制（不会请求订阅或测速）：

```bash
clashbench doctor --config examples/bench.toml
```

做一次真实测试：

```bash
clashbench run --config examples/bench.toml
```

只测试日本、香港：

```bash
clashbench run --config examples/bench.toml --regions JP,HK
```

不访问网络的端到端冒烟测试：

```bash
clashbench run --config examples/bench.toml --mock
```

重新生成最近 7 天报告与 CSV：

```bash
clashbench report --config examples/bench.toml --days 7 --output ../reports
clashbench export --config examples/bench.toml --days 7 --output ../reports/results.csv
```

重新生成某次运行的独立报告，并以它的测试条件作为趋势锚点：

```bash
clashbench report --config examples/bench.toml --days 7 --run-id <run-uuid> --output ../reports
```

默认产物：

- `data/bench.sqlite3`：完整历史和原始字段。
- `reports/results.csv`：便于 Excel、Numbers、pandas 分析。
- `reports/runs/<时间>_<run-id>.md/.html`：每次运行的永久独立报告，包括 partial/failed 运行。
- `reports/latest.md`、`reports/latest.html`：最近一次运行的单次报告，不混入历史。
- `reports/trend-7d.md`、`reports/trend-7d.html`：与最近运行测试条件一致的完整历史趋势。

## 定时连续测试

先预览 plist，不安装：

```bash
clashbench schedule render --config examples/bench.toml \
  --times 09:00,14:00,20:00,22:00,00:00 \
  --output data/local.clash-provider-bench.plist
```

确认后安装当前用户的 LaunchAgent：

```bash
clashbench schedule install --config examples/bench.toml \
  --times 09:00,14:00,20:00,22:00,00:00
```

卸载：

```bash
clashbench schedule uninstall --config examples/bench.toml
```

`launchd` 记录的是执行安装命令时的 Python 解释器，所以应在项目 `.venv` 激活后安装。连续运行 3～7 天后再看晚高峰衰减更有意义。每个对比条件独立积累趋势；中途修改测速端点、文件大小、并发、超时或升级测速内核，会自然开始一个新的条件组。Mac 睡眠时错过的固定时刻通常不会补跑；若合盖整晚，需要外接电源并按你的能耗策略允许唤醒，或醒来后手动补跑一次。

## 配置要点

完整示例见 [`examples/bench.toml`](examples/bench.toml)。关键参数：

| 参数 | 说明 |
|---|---|
| `regions` | 全局地区筛选；空数组表示全部。 |
| `engine.server_url` | 所有供应商必须相同。无 path 的服务按 `clash-speedtest` 约定使用 `/__down` 和 `/__up`。 |
| `providers.user_agent` | 可选的供应商级订阅 UA。若服务商默认返回 Base64 URI 列表，可设为 `clash.meta`。 |
| `download_size_mb` / `upload_size_mb` | 每节点流量；节点多时先用 20/10 MB，正式跑可提高。 |
| `concurrent` | 单节点下载并发。公平比较时固定，且不要同时运行别的重流量任务。 |
| `enrichment.enabled` | 为每个 provider 启动隔离 Mihomo，获取出口信息。会增加时长。 |
| `enrichment.unlock` | 开启流媒体/ChatGPT 可达性探测。结果是最佳努力信号，不等同登录后的完整解锁保证。 |

如果使用自己的测速服务器，应实现 `clash-speedtest` 所需的 `/__down` 和 `/__up`；公共 Cloudflare 端点适合快速对比，但运营商路由/CDN 边缘会影响结果。严谨对比应固定网络（Wi‑Fi/有线）、关闭大下载、避免 VPN 叠加，并保持 Mac 供电和散热条件一致。

## 数据口径

`clash-speedtest` 的“延迟”是 6 次 HTTP HEAD 的平均 TTFB，抖动是这些样本的标准差，丢包是失败样本比例。SQLite 中每次节点测试保留这一组原始聚合值；报告的 P50/P95 是指定时间窗口内“同 provider、同地区、全部节点与全部运行”的分布，而不是伪造单次测试未暴露的 6 个原始样本。

下载/上传统一存为十进制 Mbps（原 CLI 的 KiB/MiB 单位先换算为 bytes/s，再乘 8）。`available` 定义为延迟成功且丢包低于 100%；`status=ok` 还要求当前模式所需的吞吐阶段没有错误。因此“延迟可用但下载失败”的节点会计入可用节点，同时计入测速失败。测速成功率与失败率的分母都是总节点测量数。

每个指标后面的 `n` 是实际有效样本数：

- TTFB、抖动、下载和上传只使用测速成功且该数值非空的记录。
- 丢包使用所有具有丢包数值的记录，包括 100% 丢包的失败节点。
- CV 使用下载有效样本，至少 2 条才计算；定义为总体标准差 / 均值。
- 晚高峰衰减比较本机时区 20:00–23:59 与 09:00/14:00 的下载中位数；报告同时显示两个时段各自的有效样本数。负值表示晚高峰反而更快。

### 哪些历史数据可以比较

每次运行计算一个不含秘密的 `comparison_key`。以下任一项变化都会生成新的对比条件，旧数据仍保留但不会直接混入新趋势：provider 集合、测速内核版本、测速模式/端点、下载/上传大小、并发、超时、地区集合、enrichment/unlock 开关、macOS/CPU 架构、默认接口、系统代理启用类型/服务器哈希或活动 TUN/VPN 接口。

趋势只纳入同一 `comparison_key` 且运行状态为 `ok` 的数据。某个 provider 整体失败会令该次运行成为 `partial` 或 `failed`：它仍有独立单次报告和原始数据，但不会进入公平历史趋势。升级前的旧记录按原有 `config_digest + engine` 形成带 `legacy:` 前缀的独立历史组；因当时未记录完整条件，解读时应更谨慎。

出口和解锁字段仅在 enrichment 开启后产生。`ipwho.is` 请求本身经目标节点代理，因此返回目标出口。测速阶段的 `status/error` 与附加阶段的 `enrichment_status/enrichment_error` 完全分离：出口查询或 Mihomo enrichment 启动失败不会改变节点可用率、延迟或速度结果。报告不会包含订阅服务器地址、认证字段或 token。

基础设施集中度以时间窗口内“每个稳定节点键的最新出口观测”计算，避免一天测试五次就把同一个出口算作五份资源。出口覆盖不足时，独立 IP/ASN 数和集中度只能视作已观测样本，不代表服务商完整库存。

## SQLite 表

- `runs`：运行 ID、UTC 开始/结束时间、状态、对比条件 ID、公开测速参数、内核版本、地区、provider 顺序和脱敏网络环境。
- `provider_runs`：本次每个 provider 的执行顺序、开始/结束时间、状态、测量数和脱敏错误。
- `measurements`：provider、节点名、稳定散列键、地区、时间、协议类型、测速指标与状态，以及独立的 enrichment 状态、出口信息和解锁结果。

启动时会检查并增量迁移旧数据库，只增加列和表，不删除或重写历史记录。升级前仍建议先用 SQLite `.backup` 命令保存一份副本。

节点稳定键以 provider、节点服务器/端口/协议和名称做 SHA-256 截断。服务商改名或换服务器会被视为新节点；这比仅按显示名关联更不容易误合并。

## 已知限制

- 顶层 `proxies:` 节点能生成最稳定的跨时间键。仅含远端 `proxy-providers:` 的配置仍可由 `clash-speedtest` 测试，但 orchestrator 在测速前看不到 provider 内部服务器字段，稳定键会退化为 provider + 节点名。
- 对非 YAML 订阅会尝试多个成熟客户端 UA，要求服务商直接返回 Clash/Mihomo YAML。若服务商始终只提供 URI 列表，应在本机运行 subconverter 再输入生成的 YAML；工具不会把 token 发送给公共转换站。
- `clash-speedtest` 当前 TSV 不暴露每次 HEAD 的 6 个单独样本，也不暴露其内部 download/upload error 独立字段；适配器会从格式化列区分数值和错误字符串。
- `GLOBAL` 节点选择依赖 Mihomo 的 global 模式；极少数需要特殊 rule-provider 初始化的复杂配置可能无法 enrichment，但已经完成的测速结果仍会保留。
- 网络环境记录用于暴露可能的代理/TUN 干扰，不等于完整链路指纹：工具不会保存 Wi-Fi SSID、本机 IP、代理服务器明文或凭据，也无法识别运营商临时路由变化。
- ChatGPT、YouTube、Netflix 检测只代表匿名 HTTP 可达性，页面策略和登录账户区域都可能改变结果。它们默认关闭。
- 测速本身会消耗大量订阅流量。总量约为“成功节点数 × (下载 MB + 上传 MB) × 每天次数”，另加协议开销。

## 开发与验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q src
clashbench run --config examples/bench.toml --mock
```

若你用 Homebrew Python 3.13，建议按上面使用普通 `pip install .`；该版本会忽略以 `__editable__` 开头的隐藏 `.pth`，某些 setuptools 版本的 `pip install -e .` 因而可能无法导入包。

本项目不包含、不缓存任何真实订阅。运行真实测试前请确认服务商条款允许自动测速，并避免过高频率或过大并发。
