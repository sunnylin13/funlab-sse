"""SSE-12【P2】孤兒事件清理：已卸載插件的事件型別必須可被清理（IMPROVEMENT_PLAN SSE-12）.

tmp-sqlite DbMgr 小集成測試（探針同款）。
"""
import os
import tempfile

import pytest

from funlab.core.dbmgr import DbMgr
from funlab.sse.manager import EventManager
from funlab.sse.model import EventEntity, SystemNotificationEvent


@pytest.fixture()
def em():
    tmpdb = tempfile.mktemp(suffix='.db')
    db = DbMgr({'url': f'sqlite:///{tmpdb}'})
    EventManager.register_event(SystemNotificationEvent)
    EventEntity.__table__.create(bind=db.get_db_engine(), checkfirst=True)
    m = EventManager(db)
    yield m
    m.shutdown()
    db.release()
    os.unlink(tmpdb)


def test_orphan_events_purged(em):
    ev = em.create_event('SystemNotification', 1, title='t', message='m')
    assert ev is not None and ev.id is not None
    with em.dbmgr.session_context() as s:
        s.add(EventEntity(event_type='GonePlugin', payload='{"x":1}',
                          target_userid=1, priority='NORMAL'))
    em.clean_up_events()
    with em.dbmgr.session_context() as s:
        assert s.query(EventEntity).filter_by(event_type='GonePlugin').count() == 0
        # 未讀的已知型別事件保留
        assert s.query(EventEntity).filter_by(id=ev.id).count() == 1
