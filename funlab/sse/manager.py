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
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager

from .model import ServerSideEvent, ServerSideEventEntity

class ServerSideEventMgr:
    def __init__(self, dbmgr:DbMgr, max_queue_size=1000, max_client_events=100):
        self.mylogger = log.get_logger(self.__class__.__name__, level=logging.INFO)
        self.dbmgr:DbMgr = dbmgr
        self.event_queue = {'global': queue.Queue(maxsize=max_queue_size), 
                            'user': {}}  # user_id: queue.Queue(maxsize=max_queue_size)
        # self.user_event_queues = {}
        # self.active_user_streams = {}
        self.max_client_events = max_client_events
        self.lock = threading.Lock()
        self.is_shutting_down = False

        self._recover_unprocessed_events()
        self.distributor_thread = self.start_event_distributor()

        # atexit.register(self.shutdown)
        # signal.signal(signal.SIGTERM, lambda signo, frame: self.shutdown())
        # signal.signal(signal.SIGINT, lambda signo, frame: self.shutdown())

    @property
    def global_event_queue(self)->queue.Queue:
        return self.event_queue['global']
    
    @property
    def user_event_queues(self)->dict[int, queue.Queue]:
        return self.event_queue['user']
    
    @property
    def all_event_queues(self)->list[queue.Queue]:
        return list(self.user_event_queues.values()).insert(0, self.global_event_queue)

    def _recover_unprocessed_events(self):
        with self.dbmgr.session_context() as session:
            stmt = select(ServerSideEventEntity).where(ServerSideEventEntity.is_expired() == False).order_by(ServerSideEventEntity.created_at.asc())
            unprocessed_events: list[ServerSideEventEntity] = session.execute(stmt).scalars().all()
            for event_record in unprocessed_events:
                try:
                    if event_record.is_global:
                        self.global_event_queue.put_nowait(event_record.__dict__)
                    else:
                        self.user_event_queues[event_record.user_id].put_nowait(event_record.__dict__)
                except queue.Full:  # exception raised when queue is full
                    break
            session.commit()

    def _store_event(self, event: dict[str, Any]):
        with self.dbmgr.session_context() as session:
            queued_event = ServerSideEventEntity(
                event_type=event['type'],
                payload=event['payload'],
                user_id=event.get('user_id'),
                is_read=event.get('is_read', False),
                # is_global=event.get('is_global', False),
                # priority=event.get('priority', 2),
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
                except queue.Empty:  # exception raised when queue is empty
                    break
        self.mylogger.info("All unprocessed events have been saved")

    def start_event_distributor(self):
        def distributor():
            while not self.is_shutting_down:
                try:
                    for queue in self.all_event_queues:
                        while not queue.empty():
                            event = queue.get(timeout=1)
                            with self.dbmgr.session_context() as session:
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
                    self.mylogger.error(f"Event distribution error: {e}")

        thread = threading.Thread(target=distributor, daemon=True)
        thread.start()
        return thread

    def _distribute_global_event(self, event: Dict[Any, Any]):
        """
        分發全域事件到所有活躍用戶
        """
        with self.lock:
            for user_streams in self.active_user_streams.values():
                for stream in user_streams:
                    try:
                        # 防止隊列溢出
                        if stream.qsize() < self.max_client_events:
                            stream.put_nowait(event)
                    except queue.Full:
                        # 隊列已滿，丟棄最早的事件
                        stream.get_nowait()
                        stream.put_nowait(event)
    
    def _distribute_user_specific_event(self, event: Dict[Any, Any]):
        """
        分發特定用戶事件
        """
        user_id = event.get('user_id')
        if user_id is None:
            return
        
        with self.lock:
            # 檢查該用戶是否有活躍流
            if user_id in self.active_user_streams:
                for stream in self.active_user_streams[user_id]:
                    try:
                        # 防止隊列溢出
                        if stream.qsize() < self.max_client_events:
                            stream.put_nowait(event)
                    except queue.Full:
                        # 隊列已滿，丟棄最早的事件
                        stream.get_nowait()
                        stream.put_nowait(event)
                        
    def create_event(self, event_type: str, data: Dict[Any, Any], user_id: int = None, expires_in: int = 3600):
        event = {
            'type': event_type,
            'data': data,
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