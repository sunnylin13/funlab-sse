"""SSE-09：cleanup 排程在 DB 錯誤時仍要睡眠，不得形成緊迫迴圈（IMPROVEMENT_PLAN SSE-09）."""
import time

from funlab.sse.manager import EventManager


def test_cleanup_scheduler_sleeps_even_on_error(monkeypatch):
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('t')
    em.is_shutting_down = False
    calls = {'clean': 0, 'sleeps': []}

    def boom():
        calls['clean'] += 1
        em.is_shutting_down = True      # 讓迴圈本輪後退出
        raise RuntimeError('db down')

    monkeypatch.setattr(EventManager, 'clean_up_events', lambda self: boom())
    monkeypatch.setattr(time, 'sleep', lambda s: calls['sleeps'].append(s))
    t = em._start_cleanup_scheduler(interval_minutes=1)
    t.join(2)
    assert calls['clean'] == 1
    assert calls['sleeps'] == [60]      # 拋異常仍睡覺
