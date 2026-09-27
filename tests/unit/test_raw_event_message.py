"""SSE-03: RawEventMessage must satisfy the distributor field contract.

Before the fix, ``RawEventMessage`` lacked ``is_read`` / ``is_expired`` /
``target_userid``, so ``_start_event_distributor`` raised AttributeError on
every raw event, the broad except swallowed it, and ``send_event`` /
``send_raw_event`` silently dropped 100% of events while returning True.
"""
import logging
import queue

from funlab.sse.manager import ConnectionManager, EventManager, RawEventMessage


class TestRawEventMessageDistributorContract:
    def test_has_distributor_fields(self):
        m = RawEventMessage('PriceUpdate', 1, {'symbol': '2330', 'price': '100'})
        assert m.is_read is False
        assert m.is_expired is False
        assert m.target_userid == 1

    def test_to_dict_fields(self):
        m = RawEventMessage('PriceUpdate', 7, {'p': '1'}, priority='high')
        d = m.to_dict()
        assert d['id'] is None
        assert d['event_type'] == 'PriceUpdate'
        assert d['priority'] == 'HIGH'
        assert d['target_userid'] == 7
        assert d['payload'] == {'p': '1'}
        assert d['is_recovered'] is False
        assert d['created_at']  # ISO timestamp present

    def test_distribute_reaches_stream(self):
        em = EventManager.__new__(EventManager)
        em.mylogger = logging.getLogger('t')
        em.connection_manager = ConnectionManager()
        em.max_events_per_stream = 100
        sid = em.connection_manager.add_connection(1, queue.Queue(maxsize=100), 'PriceUpdate')
        q = em.connection_manager.user_connections[1][sid]
        msg = RawEventMessage('PriceUpdate', 1, {'p': '1'})
        assert not (msg.is_read or msg.is_expired)   # distributor 的准入檢查
        em._distribute_event(msg)
        assert q.qsize() == 1
        assert q.get_nowait().to_dict()['payload'] == {'p': '1'}

    def test_is_read_not_in_slots(self):
        # PLAN 紅線：is_read 用類別屬性，不得進 __slots__
        assert 'is_read' not in RawEventMessage.__slots__
        assert 'target_userid' in RawEventMessage.__slots__
