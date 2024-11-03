import queue
import threading
import json
import signal
import atexit
from datetime import datetime, timedelta
from typing import Dict, Any, Set
from flask import Flask
from funlab.core.dbmgr import DbMgr
from funlab.flaskr.app import FunlabFlask
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager

from .model import ServerSideEvent, ServerSideEventEntity

class ServerSideEventMgr:
    def __init__(self, dbmgr, max_queue_size=1000, max_client_events=100):

        self.dbmgr = dbmgr
        self.global_event_queue = queue.Queue(maxsize=max_queue_size)
        self.user_event_queues = {}
        self.active_user_streams = {}
        self.max_client_events = max_client_events
        self.lock = threading.Lock()
        self.is_shutting_down = False

        self._recover_unprocessed_events()
        self.distributor_thread = self.start_event_distributor()

        atexit.register(self.shutdown)
        signal.signal(signal.SIGTERM, lambda signo, frame: self.shutdown())
        signal.signal(signal.SIGINT, lambda signo, frame: self.shutdown())

    @contextmanager
    def get_db_session(self):
        session = self.dbmgr.get_db_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _recover_unprocessed_events(self):
        with self.get_db_session() as session:
            stmt = select(ServerSideEventEntity).where(ServerSideEventEntity.is_expired() == False).order_by(ServerSideEventEntity.created_at.asc())
            unprocessed_events = session.execute(stmt).scalars().all()
            for event_record in unprocessed_events:
                try:
                    self.global_event_queue.put_nowait(event_record.__dict__)
                    event_record.processed = True
                    event_record.processing_attempts += 1
                except queue.Full:
                    break
                session.commit()

    def _store_event(self, event: Dict[str, Any]):
        with self.get_db_session() as session:
            queued_event = ServerSideEventEntity(
                event_type=event['type'],
                payload=event['data'],
                user_id=event.get('user_id'),
                is_global=event.get('is_global', False),
                priority=event.get('priority', 2),
                expires_at=datetime.fromisoformat(event['expires_at']) if event.get('expires_at') else None
            )
            session.add(queued_event)

    def shutdown(self):
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        self.my("Shutting down event notification system...")
        while not self.global_event_queue.empty():
            try:
                event = self.global_event_queue.get_nowait()
                self._store_event(event)
            except queue.Empty:
                break
        print("All unprocessed events have been saved")

    def start_event_distributor(self):
        def distributor():
            while not self.is_shutting_down:
                try:
                    event = self.global_event_queue.get(timeout=1)
                    with self.get_db_session() as session:
                        event_record = session.query(ServerSideEventEntity).filter_by(id=event['id']).first()
                        if event_record:
                            event_record.processed = True
                            event_record.processing_attempts += 1
                    if event.get('is_global', False):
                        self._distribute_global_event(event)
                    else:
                        self._distribute_user_specific_event(event)
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"Event distribution error: {e}")

        thread = threading.Thread(target=distributor, daemon=True)
        thread.start()
        return thread

    def create_event(self, event_type: str, data: Dict[Any, Any], user_id: int = None, is_global: bool = False, priority: int = 2, expires_in: int = 3600):
        event = {
            'id': datetime.utcnow().isoformat(),
            'type': event_type,
            'data': data,
            'user_id': user_id,
            'is_global': is_global,
            'priority': priority,
            'expires_at': (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        }
        self._store_event(event)
        try:
            self.global_event_queue.put_nowait(event)
        except queue.Full:
            try:
                self.global_event_queue.get_nowait()
                self.global_event_queue.put_nowait(event)
            except:
                pass

# class FlaskSSE:
#     def __init__(self, app: Flask = None, dbmgr=None):
#         self.app = app
#         self.dbmgr = dbmgr
#         if app is not None:
#             self.init_app(app)

#     def init_app(self, app: Flask):
#         self.app = app
#         self.app.extensions['sse'] = self
#         self.app.teardown_appcontext(self.teardown)

#     def init_dbmgr(self, dbmgr):
#         self.dbmgr = dbmgr

#     def teardown(self, exception):
#         sse_mgr = g.pop('sse_mgr', None)
#         if sse_mgr is not None:
#             sse_mgr.shutdown()

#     @property
#     def sse_mgr(self):
#         if 'sse_mgr' not in g:
#             g.sse_mgr = ServerSideEventMgr(self.dbmgr)
#         return g.sse_mgr