# Repository workflow

- After completing and testing any requested code or documentation update, create a focused Git commit and push it to `origin/main` unless the user explicitly asks not to push.
- Before every commit, verify that `.env`, subscription URLs/tokens, generated databases, reports, runtime configs, downloaded binaries, and IDE state are not staged.
- Never print or commit full subscription responses. Diagnostics may report only HTTP status, content type, byte count, detected format, node count, and protocol counts.
- Run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` before pushing code changes.
