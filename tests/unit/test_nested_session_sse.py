"""SSE-06 回歸：SSE 寫入點不得提前提交外層交易（依賴 funlab-libs LIB-01 巢狀語意）。

PLAN SSE-06 驗證：外層 session_context 交易中途呼叫 SSE 寫入（create_event/
dismiss_*）後外層 raise → 全部回滾（rows=0）；外層正常結束 → 兩層一起提交。
後台執行緒（distributor/cleanup）自成執行緒、scoped_session 每執行緒一份，
不與請求共享，此路安全（PLAN (b) 事實鏈）。
"""
import inspect

import pytest
from sqlalchemy import select

from funlab.core.dbmgr import DbMgr
from funlab.sse.manager import EventManager
from funlab.sse.model import EventEntity, SystemNotificationEvent

EventManager.register_event(SystemNotificationEvent)

# SSE-06 回購依賴 funlab-libs LIB-01（A1, bbf2306）：session_context 帶 nested 參數
# ＝深度計數新語意。舊版 libs（無 nested）下內層必提交，語意不同，跳過而非誤報。
_HAS_LIB01 = 'nested' in inspect.signature(DbMgr.session_context).parameters
pytestmark = pytest.mark.skipif(
    not _HAS_LIB01,
    reason="SSE-06 regression requires funlab-libs LIB-01 (session_context nested semantics)",
)


def _bare_manager_with_db(tmp_path):
    dbmgr = DbMgr({"url": f"sqlite:///{tmp_path / 'sse06.db'}"})
    EventEntity.__table__.create(bind=dbmgr.get_db_engine(), checkfirst=True)
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('t')
    em.dbmgr = dbmgr
    import queue as _q
    import threading
    em.event_queue = _q.Queue(maxsize=1000)
    em.lock = threading.Lock()
    em.dropped_event_count = 0
    from funlab.sse.manager import ConnectionManager
    em.connection_manager = ConnectionManager()
    em.max_events_per_stream = 100
    return dbmgr, em


def _event_rows(dbmgr):
    with dbmgr.session_context() as s:
        return s.execute(select(EventEntity)).scalars().all()


def test_nested_create_event_does_not_commit_outer(tmp_path):
    """外層交易中途 send→create_event，外層 raise：SSE 事件與外層寫入一併回滾。"""
    dbmgr, em = _bare_manager_with_db(tmp_path)
    with pytest.raises(RuntimeError):
        with dbmgr.session_context() as outer:
            em.create_event('SystemNotification', 1, title='t', message='m')
            raise RuntimeError('outer abort')
    assert len(_event_rows(dbmgr)) == 0        # 探針 probe_nested 語意：rows=0


def test_nested_create_event_commits_with_outer(tmp_path):
    """外層正常結束：SSE 事件隨最外層一起提交，且 id 已回填。"""
    dbmgr, em = _bare_manager_with_db(tmp_path)
    with dbmgr.session_context():
        ev = em.create_event('SystemNotification', 1, title='t', message='m')
    rows = _event_rows(dbmgr)
    assert len(rows) == 1
    assert ev.id is not None and rows[0].id == ev.id


def test_dismiss_nested_in_outer_transaction(tmp_path):
    """dismiss_all 於外層交易中途呼叫：不提前提交；外層 abort 則一起回滾。"""
    dbmgr, em = _bare_manager_with_db(tmp_path)
    ev = em.create_event('SystemNotification', 1, title='t', message='m')  # 最外層：已提交
    assert len(_event_rows(dbmgr)) == 1
    with pytest.raises(RuntimeError):
        with dbmgr.session_context():
            # 直接重現 dismiss 的 session 巢狀行為（不構造完整 SSEService app）
            with dbmgr.session_context() as s:
                s.query(EventEntity).filter(
                    EventEntity.target_userid == 1,
                    EventEntity.is_read == False,
                ).update({'is_read': True}, synchronize_session=False)
            raise RuntimeError('outer abort')
    rows = _event_rows(dbmgr)
    assert rows[0].is_read is False            # 巢狀 dismiss 未提前提交


def test_outermost_create_event_still_persists(tmp_path):
    """非巢狀（最外層）呼叫：寫入照常落地（回歸保護）。"""
    dbmgr, em = _bare_manager_with_db(tmp_path)
    em.create_event('SystemNotification', 7, title='solo', message='m')
    rows = _event_rows(dbmgr)
    assert len(rows) == 1 and rows[0].target_userid == 7
