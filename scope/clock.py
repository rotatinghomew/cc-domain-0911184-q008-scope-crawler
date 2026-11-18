"""时间工具：统一使用时区感知的 UTC 时间，便于注入测试时钟。"""

from datetime import datetime, timezone


def now(clock=None):
    """当前 UTC 时间。clock 为 None 时使用系统时钟，否则调用 clock()。"""
    if clock is None:
        return datetime.now(timezone.utc)
    value = clock()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value):
    """UTC 时间转 ISO8601 字符串；None 原样返回；字符串视为已序列化。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value):
    """ISO8601 字符串转 UTC 时间；None 原样返回。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
