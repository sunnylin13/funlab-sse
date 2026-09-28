"""SSE-04/05/13：跨型別投遞過濾、eviction sentinel、垃圾鍵回收（IMPROVEMENT_PLAN SSE-04）."""
import queue

from funlab.sse.manager import ConnectionManager, EventManager, STREAM_CLOSED
from funlab.sse.model import SystemNotificationEvent


def test_streams_filtered_by_event_type():
    cm = ConnectionManager()
    qn, qp = queue.Queue(maxsize=50), queue.Queue(maxsize=50)
    cm.add_connection(1, qn, 'SystemNotification')
    cm.add_connection(1, qp, 'PriceUpdate')
    assert cm.get_user_streams(1, event_type='SystemNotification') == {qn}
    assert cm.get_user_streams(1, event_type='PriceUpdate') == {qp}
    assert cm.get_user_streams(1) == {qn, qp}          # 不給型別＝全部（相容舊行為）


def test_distribute_only_same_event_type():
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('t')
    em.connection_manager = ConnectionManager()
    em.max_events_per_stream = 100
    qn, qp = queue.Queue(maxsize=50), queue.Queue(maxsize=50)
    em.connection_manager.add_connection(1, qn, 'SystemNotification')
    em.connection_manager.add_connection(1, qp, 'PriceUpdate')
    ev = SystemNotificationEvent(target_userid=1, title='t', message='m')
    em._distribute_event(ev)
    assert qn.qsize() == 1 and qp.qsize() == 0


def test_evicted_stream_receives_sentinel():
    cm = ConnectionManager(max_connections_per_user=1)
    q1 = queue.Queue(maxsize=50)
    cm.add_connection(2, q1, 'E')
    cm.add_connection(2, queue.Queue(maxsize=50), 'E')   # 踢掉 q1
    assert q1.qsize() == 1 and q1.get_nowait() is STREAM_CLOSED


def test_eventtype_key_purged_when_empty():
    cm = ConnectionManager()
    sid = cm.add_connection(3, queue.Queue(), 'JUNK')
    cm.remove_connection(3, sid, 'JUNK')
    assert 'JUNK' not in cm.eventtype_connection_users
