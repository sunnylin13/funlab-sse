import queue, sys, pytest

# 加入 workspace funlab-libs 到 sys.path，使用最新版 funlab.core
_ws_funlab_libs = r"d:\08.dev\fundlife\funlab-libs"
if _ws_funlab_libs not in sys.path:
    sys.path.insert(0, _ws_funlab_libs)

from funlab.sse.manager import ConnectionManager


def make_stream():
    return queue.Queue(maxsize=50)


class TestAddRemoveConnection:
    def test_add_connection_returns_stream_id(self):
        mgr = ConnectionManager()
        sid = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        assert isinstance(sid, str) and len(sid) > 0

    def test_user_appears_in_connections_after_add(self):
        mgr = ConnectionManager()
        mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        assert 1 in mgr.user_connections

    def test_user_removed_from_connections_after_disconnect(self):
        mgr = ConnectionManager()
        sid = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        mgr.remove_connection(user_id=1, stream_id=sid, event_type="E")
        assert 1 not in mgr.user_connections

    def test_remove_nonexistent_does_not_raise(self):
        mgr = ConnectionManager()
        mgr.remove_connection(user_id=99, stream_id="nonexistent", event_type="X")

    def test_connect_time_cleared_after_disconnect(self):
        mgr = ConnectionManager()
        sid = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        assert sid in mgr.users_connect_time
        mgr.remove_connection(user_id=1, stream_id=sid, event_type="E")
        assert sid not in mgr.users_connect_time


class TestRemoveAllConnections:
    def test_remove_all_clears_all_streams_for_user(self):
        mgr = ConnectionManager()
        mgr.add_connection(user_id=5, stream=make_stream(), event_type="E1")
        mgr.add_connection(user_id=5, stream=make_stream(), event_type="E1")
        assert len(mgr.user_connections[5]) == 2
        mgr.remove_all_connections(user_id=5)
        assert 5 not in mgr.user_connections

    def test_remove_all_clears_connect_times(self):
        mgr = ConnectionManager()
        sid1 = mgr.add_connection(user_id=5, stream=make_stream(), event_type="E1")
        sid2 = mgr.add_connection(user_id=5, stream=make_stream(), event_type="E1")
        mgr.remove_all_connections(user_id=5)
        assert sid1 not in mgr.users_connect_time
        assert sid2 not in mgr.users_connect_time

    def test_remove_all_nonexistent_user_does_not_raise(self):
        mgr = ConnectionManager()
        mgr.remove_all_connections(user_id=999)

    def test_remove_all_does_not_affect_other_users(self):
        mgr = ConnectionManager()
        mgr.add_connection(user_id=5, stream=make_stream(), event_type="E1")
        mgr.add_connection(user_id=7, stream=make_stream(), event_type="E1")
        mgr.remove_all_connections(user_id=5)
        assert 7 in mgr.user_connections


class TestMaxConnectionEviction:
    def test_oldest_connection_evicted_at_limit(self):
        import time
        mgr = ConnectionManager(max_connections_per_user=2)
        sid1 = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        time.sleep(0.01)
        sid2 = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        time.sleep(0.01)
        sid3 = mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        conns = mgr.user_connections[1]
        assert sid1 not in conns
        assert sid2 in conns and sid3 in conns

    def test_connection_count_stays_at_max(self):
        mgr = ConnectionManager(max_connections_per_user=3)
        for _ in range(5):
            mgr.add_connection(user_id=1, stream=make_stream(), event_type="E")
        assert len(mgr.user_connections[1]) == 3


class TestNoEventsAfterDisconnect:
    def test_no_streams_after_remove_connection(self):
        mgr = ConnectionManager()
        sid = mgr.add_connection(user_id=10, stream=make_stream(), event_type="E")
        mgr.remove_connection(user_id=10, stream_id=sid, event_type="E")
        assert len(mgr.get_user_streams(user_id=10)) == 0

    def test_no_streams_after_remove_all(self):
        mgr = ConnectionManager()
        for _ in range(3):
            mgr.add_connection(user_id=10, stream=make_stream(), event_type="E")
        mgr.remove_all_connections(user_id=10)
        assert len(mgr.get_user_streams(user_id=10)) == 0

    def test_eventtype_users_cleared_after_all_disconnect(self):
        mgr = ConnectionManager()
        sid = mgr.add_connection(user_id=42, stream=make_stream(), event_type="SystemNotification")
        assert 42 in mgr.get_eventtype_users("SystemNotification")
        mgr.remove_connection(user_id=42, stream_id=sid, event_type="SystemNotification")
        assert 42 not in mgr.get_eventtype_users("SystemNotification")