"""Rate limiter برای جلوگیری از حملات brute-force روی لاگین"""

from datetime import datetime, timezone, timedelta
from threading import Lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RateLimiter:
    """
    In-memory rate limiter با پاک‌سازی خودکار

    - اگه کاربری در بازه‌ی window از حد مجاز رد بشه، مسدود می‌شه
    - کلیدهایی که از آخرین درخواستشون بیش از cleanup_age گذشته، حذف می‌شن
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_minutes: int = 15,
        cleanup_minutes: int = 30,
    ):
        self.max_attempts = max_attempts
        self.window = timedelta(minutes=window_minutes)
        self.cleanup_age = timedelta(minutes=cleanup_minutes)
        self._attempts: dict[str, list[datetime]] = {}
        self._last_seen: dict[str, datetime] = {}
        self._lock = Lock()

    def _cleanup(self, now: datetime) -> None:
        """پاک‌سازی IPهایی که از آخرین درخواستشون cleanup_age گذشته"""
        stale = [k for k, t in self._last_seen.items() if now - t > self.cleanup_age]
        for k in stale:
            self._attempts.pop(k, None)
            self._last_seen.pop(k, None)

    def is_allowed(self, key: str) -> bool:
        with self._lock:
            now = _utcnow()
            self._cleanup(now)
            attempts = [t for t in self._attempts.get(key, []) if now - t < self.window]
            self._attempts[key] = attempts
            if attempts:
                self._last_seen[key] = attempts[-1]
            return len(attempts) < self.max_attempts

    def record(self, key: str) -> None:
        with self._lock:
            now = _utcnow()
            self._attempts.setdefault(key, []).append(now)
            self._last_seen[key] = now

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._last_seen.pop(key, None)

    def remaining_seconds(self, key: str) -> int:
        with self._lock:
            now = _utcnow()
            attempts = [t for t in self._attempts.get(key, []) if now - t < self.window]
            if len(attempts) < self.max_attempts:
                return 0
            return max(0, int((self.window - (now - min(attempts))).total_seconds()))
