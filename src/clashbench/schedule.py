from __future__ import annotations

import plistlib
import sys
from pathlib import Path


LABEL = "local.clash-provider-bench"


def launch_agent(
    config: Path, project: Path, times: list[str], python: Path | None = None,
    mode: str = "two-stage", regions: str | None = None,
) -> bytes:
    if mode not in {"two-stage", "quick", "full"}:
        raise ValueError(f"Invalid schedule mode: {mode}")
    calendar = []
    for value in times:
        hour, minute = (int(x) for x in value.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid schedule time: {value}")
        calendar.append({"Hour": hour, "Minute": minute})
    python = python or Path(sys.executable)
    command = [
        "/usr/bin/caffeinate", "-i", str(python), "-m", "clashbench.cli",
        "run", "--config", str(config),
    ]
    if mode == "quick":
        command.append("--quick")
    elif mode == "two-stage":
        command.append("--two-stage")
    if regions:
        command.extend(["--regions", regions])
    payload = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(project),
        "StartCalendarInterval": calendar, "RunAtLoad": False,
        "StandardOutPath": str(project / "data" / "launchd.stdout.log"),
        "StandardErrorPath": str(project / "data" / "launchd.stderr.log"),
        "ProcessType": "Standard", "Umask": 0o077,
    }
    return plistlib.dumps(payload, sort_keys=False)
