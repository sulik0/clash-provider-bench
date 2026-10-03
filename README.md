# Clash Provider Bench

Clash Provider Bench 可以在 macOS 上自动测试多个 Clash/Mihomo 订阅服务商的节点，帮助你比较延迟、下载和上传速度，以及哪些节点能访问 ChatGPT。程序调用 `faceair/clash-speedtest` 和 Mihomo 完成测试，还可以连接 OpenAI Realtime API，检查 WebSocket 能否保持连接。结果保存为 SQLite、CSV、Markdown 和 HTML；订阅链接和 token（访问令牌）不会写入日志或报告。

测试怎么做、参数怎么设置、报告里的数字怎么算，见 [详细说明](docs/guide.md)。

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

复制环境变量示例文件，再填入自己的订阅链接：

```bash
cp examples/.env.example examples/.env
chmod 600 examples/.env
```

```dotenv
TAISHAN_SUB_URL=https://example.com/your-secret-subscription
COKECLOUD_SUB_URL=https://example.com/your-secret-subscription
# 可选：填入 API key 后，程序会检查 Realtime API 会话能否保持连接、断开后能否重连
OPENAI_API_KEY=
```

随后编辑 `examples/bench.toml`，设置服务商、测试地区和测速参数。每个服务商对应一个 `[[providers]]` 配置项，也可以使用本地 YAML 文件：

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

用示例数据试运行，熟悉命令和报告格式：

```bash
clashbench run --config examples/bench.toml --mock
```

重新生成报告和 CSV：

```bash
clashbench report --config examples/bench.toml --days 7
clashbench export --config examples/bench.toml
```

设置每天自动测试，并试发一条 macOS 通知：

```bash
clashbench schedule install --config examples/bench.toml --regions JP,SG,US
clashbench notify --config examples/bench.toml --test
```

默认查看 `reports/latest.md` 可以看到最近一次结果，`reports/trend-7d.md` 展示最近 7 天的数据。每次测试的报告也会单独保存在 `reports/runs/`。更多命令和报告说明见 [详细说明](docs/guide.md)。
