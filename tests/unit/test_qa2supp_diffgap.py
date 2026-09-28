"""[QA2-補測 t_d721078d] Wave2 diff 缺口——funlab-sse。

覆蓋點：
- manager._signal_close 佇列滿時的腾格重投分支（142-145）
- service 流式 generator：STREAM_CLOSED sentinel 體面退出（354/355/357）
  與 finally 的 sse_mgr 競態防護（372-374，SSE-13）
"""
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import flask
from flask import Blueprint

from funlab.sse.manager import (ConnectionManager, RawEventMessage,
                                STREAM_CLOSED)
from funlab.sse.service import SSEService


# ---------------------------------------------------------------- manager
def test_signal_close_drains_full_queue_then_signals():
    """被淘汰連線的佇列已滿：先 get_nowait 腾一格，再投 sentinel（142-145）。"""
    q = queue.Queue(maxsize=1)
    q.put('stale-payload')
    ConnectionManager._signal_close(q)
    assert q.qsize() == 1
    assert q.get_nowait() is STREAM_CLOSED


# ---------------------------------------------------------------- service
def _make_stream_app(fake_events):
    """構造最小 SSEService：真 ConnectionManager ＋ 假 EventManager。

    fake_events: 預載進新連線佇列的事件清單（generator 逐條消費）。
    """
    cm = ConnectionManager()
    unregistered = []

    class FakeMgr:
        def __init__(self):
            self.connection_manager = cm

        def register_user_stream(self, user_id, event_type):
            q = queue.Queue(maxsize=10)
            sid = cm.add_connection(user_id, q, event_type)
            for ev in fake_events:
                q.put_nowait(ev)
            return sid

        def unregister_user_stream(self, user_id, stream_id, event_type):
            unregistered.append((user_id, stream_id, event_type))
            cm.remove_connection(user_id, stream_id, event_type)

    svc = SSEService.__new__(SSEService)
    svc.app = MagicMock()
    svc._blueprint = Blueprint('sse_probe', __name__)   # blueprint 為 property → 設底層屬性
    svc.sse_mgr = FakeMgr()
    svc._register_sse_routes()

    app = flask.Flask(__name__)
    app.register_blueprint(svc._blueprint)
    return app, svc, unregistered


def test_stream_exits_on_sentinel_and_unregisters(monkeypatch):
    """收到 STREAM_CLOSED → yield 完事件後體面 return（354-357），
    finally 正常委派 unregister（374）。"""
    import funlab.sse.service as svc_mod
    monkeypatch.setattr(svc_mod, 'current_user', SimpleNamespace(id=7))

    ev = RawEventMessage('TICK', 7, {'px': 1.0})
    app, svc, unregistered = _make_stream_app([ev, STREAM_CLOSED])

    resp = app.test_client().get('/TICK')
    body = resp.get_data(as_text=True)
    assert 'event: TICK' in body
    assert 'heartbeat' not in body          # sentinel 直接終止，不會滯留等 Empty
    assert len(unregistered) == 1           # finally 帶 mgr 時照常卸載
    assert unregistered[0][0] == 7


def test_stream_finally_tolerates_mgr_none(monkeypatch):
    """SSE-13：generator 終止瞬間 sse_mgr 已被 _on_stop 置 None →
    finally 跳過卸載、不拋 AttributeError（372-373 None 分支）。"""
    import funlab.sse.service as svc_mod
    monkeypatch.setattr(svc_mod, 'current_user', SimpleNamespace(id=7))

    ev = RawEventMessage('TICK', 7, {'px': 2.0})
    app, svc, unregistered = _make_stream_app([ev])

    with app.test_request_context('/TICK'):
        resp = app.view_functions['sse_probe.stream_events']('TICK')
        gen = resp.response
        first = next(gen)                    # 產出一條事件後停在 yield
        svc.sse_mgr = None                   # 模擬 _on_stop 競態
        gen.close()                          # GeneratorExit → finally
    assert 'event: TICK' in first
    assert unregistered == []                # mgr 為 None：跳過，未拋錯
