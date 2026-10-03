# Repository workflow

- After completing and testing any requested code or documentation update, create a focused Git commit and push it to `origin/main` unless the user explicitly asks not to push.
- Before every commit, verify that `.env`, subscription URLs/tokens, generated databases, reports, runtime configs, downloaded binaries, and IDE state are not staged.
- Never print or commit full subscription responses. Diagnostics may report only HTTP status, content type, byte count, detected format, node count, and protocol counts.
- Run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` before pushing code changes.

## 中文文档写法

- README、使用说明、配置注释和自动生成的报告都优先使用自然、具体的中文，写清楚谁做什么、为什么做、做到什么程度。
- 少用连续名词短语，不把英文技术概念逐字翻译成抽象中文。避免“XX 驱动”“XX 职责”“边际价值”“有限动作集”“可核查低于……”这类压缩表达。
- 英文术语确有帮助时，保留 English（中文解释）。配置名、命令、数据库字段和结果状态保持原样，并在正文中解释含义。
