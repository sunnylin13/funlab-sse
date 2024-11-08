import logging
import queue
import threading
import json
import signal
import atexit
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Set
from flask import Flask
from funlab.core.dbmgr import DbMgr
from funlab.flaskr.app import FunlabFlask
from funlab.utils import log
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from contextlib import contextmanager

from .model import ServerSideEvent, ServerSideEventEntity

class EventStorageSystem:
    def __init__(self, db_url):
        self.engine = create_engine(db_url)
        self.Session = scoped_session(sessionmaker(bind=self.engine))

    
    @contextmanager
    def session_scope(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def store_event(self, event_data: dict):
        with self.session_scope() as session:
            queued_event = ServerSideEventEntity(
                event_type=event_data['type'],
                payload=event_data['payload'],
                user_id=event_data.get('user_id'),
                is_global=event_data.get('is_global', False),
                priority=event_data.get('priority', 2),
                expires_at=datetime.fromisoformat(event_data['expires_at'])
                    if 'expires_at' in event_data else None
            )
            session.add(queued_event)
    
    def load_pending_events(self):
        with self.session_scope() as session:
            return session.query(ServerSideEventEntity).filter_by(
                status=EventStatus.PENDING
            ).order_by(ServerSideEventEntity.priority.desc()).all()
    
    def mark_event_delivered(self, event_id):
        with self.session_scope() as session:
            event = session.query(ServerSideEventEntity).get(event_id)
            if event:
                event.status = EventStatus.DELIVERED
                event.delivered_at = datetime.utcnow()
class ServerSideEventMgr:
    def __init__(self, dbmgr:DbMgr, max_queue_size=1000, max_client_events=100):
        self.mylogger = log.get_logger(self.__class__.__name__, level=logging.INFO)
        self.dbmgr:DbMgr = dbmgr
        self.global_event_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self.user_event_queues: dict[int, queue.Queue] = {}
        self.active_user_streams: dict[int, Set[queue.Queue]] = {}
        self.max_client_events = max_client_events
        self.lock = threading.Lock()
        self.is_shutting_down = False
        self._recover_stored_events()
        self.distributor_thread = self.start_event_distributor()

    @property
    def all_event_queues(self)->list[queue.Queue]:
        return list(self.user_event_queues.values()).insert(0, self.global_event_queue)

    def _recover_stored_events(self):
        with self.dbmgr.session_context() as session:
            stmt = select(ServerSideEventEntity).where(ServerSideEventEntity.is_expired() == False).order_by(ServerSideEventEntity.created_at.asc())
            unprocessed_events: list[ServerSideEventEntity] = session.execute(stmt).scalars().all()
            for event_record in unprocessed_events:
                try:
                    if event_record.is_global:
                        self.global_event_queue.put_nowait(event_record.__dict__)
                    else:
                        self.user_event_queues[event_record.user_id].put_nowait(event_record.__dict__)
                except queue.Full:
                    break
            session.commit()

    def _distribute_global_event(self, event: dict[Any, Any]):
        with self.lock:
            for user_streams in self.active_user_streams.values():
                for stream in user_streams:
                    try:
                        if stream.qsize() < self.max_client_events:
                            stream.put_nowait(event)
                    except queue.Full:
                        stream.get_nowait()
                        stream.put_nowait(event)

    def _distribute_user_specific_event(self, event: Dict[Any, Any]):
        user_id = event.get('user_id')
        if user_id is None:
            return

        with self.lock:
            if user_id in self.active_user_streams:
                for stream in self.active_user_streams[user_id]:
                    try:
                        if stream.qsize() < self.max_client_events:
                            stream.put_nowait(event)
                    except queue.Full:
                        stream.get_nowait()
                        stream.put_nowait(event)

    def _store_event(self, event: dict[str, Any]):
        with self.dbmgr.session_context() as session:
            queued_event = ServerSideEventEntity(
                event_type=event['type'],
                payload=event['payload'],
                user_id=event.get('user_id'),
                is_read=event.get('is_read', False),
                expires_at=datetime.fromisoformat(event['expires_at']) if event.get('expires_at') else None
            )
            session.add(queued_event)

    def shutdown(self):
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        self.mylogger.info("Shutting down event notification system...")
        for queue in self.all_event_queues:
            while not queue.empty():
                try:
                    event = queue.get_nowait()
                    self._store_event(event)
                except queue.Empty:
                    break
        self.mylogger.info("All unprocessed events have been saved")

    def start_event_distributor(self):
        def distributor():
            while not self.is_shutting_down:
                try:
                    for queue in self.all_event_queues:
                        while not queue.empty():
                            event: ServerSideEvent = queue.get(timeout=1)
                            if event.is_global():
                                self._distribute_global_event(event)
                            else:
                                self._distribute_user_specific_event(event)
                except queue.Empty:
                    continue
                except Exception as e:
                    self.mylogger.error(f"Event distribution error: {e}")

        distributor_thread = threading.Thread(target=distributor, daemon=True)
        distributor_thread.start()
        return distributor_thread

    def register_user_stream(self, user_id: int) -> queue.Queue:
        with self.lock:
            user_stream = queue.Queue(maxsize=self.max_client_events)
            if user_id not in self.active_user_streams:
                self.active_user_streams[user_id] = set()
            self.active_user_streams[user_id].add(user_stream)
            return user_stream

    def unregister_user_stream(self, user_id: int, stream: queue.Queue):
        with self.lock:
            if user_id in self.active_user_streams:
                self.active_user_streams[user_id].discard(stream)
                if not self.active_user_streams[user_id]:
                    del self.active_user_streams[user_id]

    def create_event(self, event_type: str, payload: Dict[Any, Any], user_id: int = None, expires_in: int = 3600):
        event = {
            'type': event_type,
            'payload': payload,
            'user_id': user_id,
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
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