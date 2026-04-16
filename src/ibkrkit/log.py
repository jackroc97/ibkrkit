import inspect
import sys
from datetime import datetime, timedelta
from enum import Enum


class LogTag(Enum):
    INFO = "INFO"
    TICK = "TICK"
    ORDR = "ORDR"
    FILL = "FILL"
    WARN = "WARN"


_last_progress_line_count = 0
_timeout_registry: dict[str, datetime] = {}


def log(tag: LogTag, message: str, current_progress: int = None, max_progress: int = None, timeout: timedelta = None) -> None:
    global _last_progress_line_count

    if timeout is not None:
        caller = inspect.stack()[1]
        key = f"{caller.filename}:{caller.lineno}"
        now = datetime.now()
        if key in _timeout_registry and now - _timeout_registry[key] < timeout:
            return
        _timeout_registry[key] = now

    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"{timestamp} [{tag.value}] "
    indent = " " * len(prefix)
    lines = message.splitlines()
    formatted = prefix + ("\n" + indent).join(lines)

    if current_progress is not None and max_progress is not None and max_progress > 0:
        bar_width = 30
        filled = int(bar_width * current_progress / max_progress)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = int(100 * current_progress / max_progress)
        progress_line = f"{indent}{bar}  {current_progress}/{max_progress} ({pct}%)"
        output = formatted + "\n" + progress_line
        total_lines = len(lines) + 1
    else:
        output = formatted
        total_lines = len(lines)

    is_progress_update = current_progress is not None and current_progress != 0

    if is_progress_update and _last_progress_line_count > 0:
        sys.stdout.write(f"\033[{_last_progress_line_count}A\033[J")

    _last_progress_line_count = total_lines if (current_progress is not None and max_progress is not None) else 0

    print(output)
