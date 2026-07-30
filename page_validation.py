from __future__ import annotations

from typing import Any

MAX_SQLITE_INTEGER = 2**63 - 1


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label}无效")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError(f"{label}无效")
    if parsed <= 0 or parsed > MAX_SQLITE_INTEGER:
        raise ValueError(f"{label}无效")
    return parsed
