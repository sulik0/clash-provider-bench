from __future__ import annotations

import plistlib
import sys
from pathlib import Path


LABEL = "local.clash-provider-bench"


def launch_agent(config: Path, project: Path, times: list[str], python: Path | None = None) -> bytes:
    calendar = []
    for value in times:
        hour, minute = (int(x) for x in value.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid schedule time: {value}")
        calendar.append({"Hour": hour, "Minute": minute})
    python = python or Path(sys.executable)
    command = [str(python), "-m", "clashbench.cli", "run", "--config", str(config)]
    payload = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(project),
        "StartCalendarInterval": calendar, "RunAtLoad": False,
        "StandardOutPath": str(project / "data" / "launchd.stdout.log"),
        "StandardErrorPath": str(project / "data" / "launchd.stderr.log"),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload, sort_keys=False)

