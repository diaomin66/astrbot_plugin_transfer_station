from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DURATION_PATTERN = re.compile(r"^\+?(?P<value>\d+)(?P<unit>[smhd])$")
MAX_DURATION_SECONDS = 10 * 365 * 86400
MAX_DISPLAY_AMOUNT = Decimal(1000000000000)
MAX_AMOUNT_DECIMAL_PLACES = 8
MAX_WINNER_COUNT = 10000
MAX_TOTAL_WINNERS = 10000
MAX_PRIZE_COUNT = 100
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 2000
MAX_KEYWORD_LENGTH = 50
MAX_PRIZE_NAME_LENGTH = 80
MAX_REASON_LENGTH = 300


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_duration(value: str) -> timedelta:
    match = _DURATION_PATTERN.fullmatch(str(value).strip().lower())
    if not match:
        raise ValueError("时长格式应为 30m、2h 或 1d")
    amount = int(match.group("value"))
    if amount <= 0:
        raise ValueError("时长必须大于 0")
    unit = match.group("unit")
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError("时长不能超过 10 年")
    return timedelta(seconds=seconds)


def parse_time_spec(value: str, *, now: datetime | None = None) -> datetime:
    normalized = str(value).strip()
    current = (now or utc_now()).astimezone(UTC)
    if normalized.lower() == "now":
        return current.replace(microsecond=0)
    if normalized.startswith("+"):
        return (current + parse_duration(normalized)).replace(microsecond=0)
    try:
        local_value = datetime.strptime(
            normalized,
            "%Y-%m-%dT%H:%M",
        ).replace(tzinfo=SHANGHAI_TZ)
    except ValueError as exc:
        raise ValueError("时间应为 YYYY-MM-DDTHH:mm、now 或 +30m/+2h/+1d") from exc
    return local_value.astimezone(UTC)


def format_shanghai(value: datetime | str | None) -> str:
    if value is None or value == "":
        return "-"
    parsed = from_iso(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")


def parse_positive_decimal(value: str) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("额度必须是有效数字") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("额度必须大于 0")
    if amount > MAX_DISPLAY_AMOUNT:
        raise ValueError("额度不能超过 1000000000000")
    decimal_places = max(0, -amount.as_tuple().exponent)
    if decimal_places > MAX_AMOUNT_DECIMAL_PLACES:
        raise ValueError(f"额度最多保留 {MAX_AMOUNT_DECIMAL_PLACES} 位小数")
    return amount


def decimal_text(value: Decimal | str | int) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    normalized = format(amount.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def new_serial(prefix: str) -> str:
    stamp = utc_now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}{stamp}{secrets.token_hex(3).upper()}"


def validate_text(
    value: str,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    normalized = str(value).strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{label}不能超过 {maximum} 个字符")
    return normalized


def is_reserved_lottery_keyword(keyword: str, claim_phrase: str) -> bool:
    normalized = str(keyword).strip()
    return (
        normalized == str(claim_phrase).strip()
        or normalized in {"确认 抽奖", "取消 抽奖", "确认 补偿", "取消 补偿"}
        or re.fullmatch(r"(?:抽奖|补偿)\s+\d+", normalized) is not None
    )


@dataclass(frozen=True, slots=True)
class ActionResult:
    key: str
    placeholders: dict[str, str] = field(default_factory=dict)
    stop: bool = True
    should_announce: bool = True
