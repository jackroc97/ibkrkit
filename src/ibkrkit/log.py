from datetime import datetime
from enum import Enum


class LogTag(Enum):
    INFO = "INFO"
    TICK = "TICK"
    ORDR = "ORDR"
    FILL = "FILL"
    WARN = "WARN"


def log(tag: LogTag, message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{timestamp}] [{tag.value}] "
    indent = " " * len(prefix)
    formatted = prefix + ("\n" + indent).join(message.splitlines())
    print(formatted)
