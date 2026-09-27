# Clash Provider Bench

Clash Provider Bench 是一个面向 macOS 的 Clash/Mihomo 订阅自动化评测工具。它复用 `faceair/clash-speedtest` 和 Mihomo，统一测试多个 provider 的节点可用率、延迟、抖动、丢包、上下行速度及出口信息，并重点识别每个节点能否访问 ChatGPT。运行时会显示 provider、测速阶段和逐节点检测进度，并生成单次报告与 3～7 天趋势报告。

项目支持订阅 URL 和本地 YAML，测试结果保存在 SQLite、CSV、Markdown 和 HTML 中。订阅 token 不会写入日志或报告。测试原理、配置项、统计口径及长期对比规则见 [详细说明](docs/guide.md)。

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

重新生成报告和 CSV，或查看某次独立运行：

```bash
clashbench report --config examples/bench.toml --days 7
clashbench export --config examples/bench.toml
clashbench report --config examples/bench.toml --run-id <run-id>
```

安装或移除 macOS 定时任务。锁屏且 Mac 保持唤醒时会按时运行，睡眠期间错过的时刻会在下次唤醒时合并补跑一次：

```bash
clashbench schedule install --config examples/bench.toml --regions JP,SG,US
# 默认使用两阶段评测；也可改为全节点完整测速或仅快速检查：
clashbench schedule install --config examples/bench.toml --full
clashbench schedule install --config examples/bench.toml --quick-only
clashbench schedule uninstall --config examples/bench.toml
```

发送一条测试通知，或重新发送最近一次评测摘要：

```bash
clashbench notify --config examples/bench.toml --test
clashbench notify --config examples/bench.toml
```

默认输出位于：

- `reports/latest.md` / `reports/latest.html`：最近一次结果
- `reports/runs/`：每次运行的独立报告
- `reports/trend-7d.md` / `reports/trend-7d.html`：长期趋势
- `reports/results.csv`：CSV 数据
- `data/bench.sqlite3`：完整历史数据库

更多配置与报告解读请阅读 [docs/guide.md](docs/guide.md)。
