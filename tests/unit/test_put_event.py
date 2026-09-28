"""SSE-01：_put_event 非阻塞化＋丢棄計數（IMPROVEMENT_PLAN SSE-01）."""
import queue
import threading

from funlab.sse.manager import ConnectionManager, EventManager
from funlab.sse.model import SystemNotificationEvent

EventManager.register_event(SystemNotificationEvent)


def _bare_manager(queue_maxsize=1):
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('test')
    em.event_queue = queue.Queue(maxsize=queue_maxsize)
    em.lock = threading.Lock()
    em.dropped_event_count = 0
    em.connection_manager = ConnectionManager()
    em.max_events_per_stream = 100
    em._store_event = lambda e: None          # 不碰 DB
    return em


class TestPutEventNonBlocking:
    def test_put_event_returns_true_when_space(self):
        em = _bare_manager()
        assert em._put_event(object()) is True

    def test_put_event_full_returns_false_without_blocking(self):
        em = _bare_manager(queue_maxsize=1)
        assert em._put_event(object()) is True
        t = threading.Thread(target=lambda: setattr(em, '_r', em._put_event(object())))
        t.start(); t.join(0.5)
        assert not t.is_alive()               # 不再永久阻塞
        assert not em.lock.locked()           # 不再持鎖
        assert em._r is False
        assert em.dropped_event_count == 1

    def test_create_event_returns_none_when_queue_full(self):
        em = _bare_manager(queue_maxsize=1)
        em.connection_manager.add_connection(1, queue.Queue(maxsize=50), 'SystemNotification')
        em.event_queue.put(object())          # 先塞滿
        assert em.create_event('SystemNotification', 1, title='t', message='m') is None
        assert em.dropped_event_count == 1
