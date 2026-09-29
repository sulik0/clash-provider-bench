# Clash Provider Bench

Clash Provider Bench 是一个面向 macOS 的 Clash/Mihomo 订阅自动化评测工具。它复用 `faceair/clash-speedtest` 和 Mihomo，比较多个 provider 的网络质量，并重点区分 ChatGPT 基础网页可达、WebSocket 握手成功和长连接稳定。结果保存为 SQLite、CSV、Markdown 和 HTML，订阅 token 不写入日志或报告。

测试原理、完整配置、统计口径和报告解读见 [详细说明](docs/guide.md)。

## 安装

要求：macOS、Python 3.11 或更高版本。

```bash
git clone git@github.com:sulik0/clash-provider-bench.git
cd clash-provider-bench

python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/install_tools.py
```

复制环境变量示例，并填入自己的订阅 URL：

```bash
cp examples/.env.example examples/.env
chmod 600 examples/.env
```

```dotenv
TAISHAN_SUB_URL=https://example.com/your-secret-subscription
COKECLOUD_SUB_URL=https://example.com/your-secret-subscription
# 可选：启用需要账号态的 ChatGPT WebSocket 稳定性测试
CHATGPT_ACCESS_TOKEN=
```

随后按需编辑 `examples/bench.toml` 中的 provider、地区和测速参数。本地配置文件也可以直接作为 provider：

```toml
[[providers]]
name = "local-snapshot"
path = "./private/provider.yaml"
```

## 使用

先检查依赖和配置：

```bash
clashbench doctor --config examples/bench.toml
```

运行全部已配置地区，或只测指定地区：

```bash
clashbench run --config examples/bench.toml
clashbench run --config examples/bench.toml --two-stage
clashbench run --config examples/bench.toml --regions JP,HK
clashbench run --config examples/bench.toml --quick
```

不访问真实订阅的演练：

```bash
clashbench run --config examples/bench.toml --mock
```

重新生成报告和 CSV：

```bash
clashbench report --config examples/bench.toml --days 7
clashbench export --config examples/bench.toml
```

安装每天定时运行和 macOS 通知：

```bash
clashbench schedule install --config examples/bench.toml --regions JP,SG,US
clashbench notify --config examples/bench.toml --test
```

默认最新结果是 `reports/latest.md`，3～7 天趋势是 `reports/trend-7d.md`。更多命令和解读见 [详细说明](docs/guide.md)。
