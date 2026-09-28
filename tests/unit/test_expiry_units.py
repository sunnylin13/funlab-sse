"""SSE-08：expire_after 單位＝秒（介面宣稱秒、實作曾按分鐘）（IMPROVEMENT_PLAN SSE-08）."""
from datetime import datetime, timezone, timedelta

from funlab.sse.manager import EventManager


class TestExpiryUnits:
    def test_expire_after_is_seconds(self):
        exp = EventManager._expiry_from_now(3600)
        assert exp - datetime.now(timezone.utc) > timedelta(seconds=3590)
        assert exp - datetime.now(timezone.utc) < timedelta(seconds=3610)

    def test_none_and_nonpositive_means_never_expire(self):
        assert EventManager._expiry_from_now(None) is None
        assert EventManager._expiry_from_now(0) is None
        assert EventManager._expiry_from_now(-5) is None
