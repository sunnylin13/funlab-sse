"""
SSE EventManager & ConnectionManager for funlab-sse plugin.

Ported and fixed from funlab-flaskr/funlab/flaskr/sse/manager.py:
  Bug fixes applied:
  - clean_up_events(): Python `or` replaced with SQLAlchemy `|` operator
  - remove_all_connections(): UUID stream_id cannot start with user_id;
    all orphaned connect-time entries are now purged on user disconnect.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from funlab.core.dbmgr import DbMgr
from funlab.utils import log
from sqlalchemy import select

from .model import EventBase, EventEntity, EventPriority


# ---------------------------------------------------------------------------
# Lightweight ephemeral event (no DB, no class registry)
# ---------------------------------------------------------------------------

class RawEventMessage:
    """Minimal event wrapper for ephemeral / real-time events (e.g. price ticks)
    that do **not** need DB persistence or a registered EventBase subclass.

    The SSE stream handler only needs ``.event_type`` and ``.to_dict()``,
    so this lightweight object is fully compatible.
    """

    __slots__ = ('event_type', '_data')

    def __init__(self, event_type: str, target_userid: int, payload: dict, priority: str = 'NORMAL'):
        self.event_type = event_type
        self._data = {
            'id': None,
            'event_type': event_type,
            'priority': priority.upper(),
            'target_userid': target_userid,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'payload': payload,
            'is_recovered': False,
        }

    def to_dict(self) -> dict:
        return self._data


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Manages per-user SSE stream connections.

    Each connection is identified by a UUID ``stream_id`` and assigned to a
    specific ``event_type``.  Multiple concurrent connections per user
    (e.g. multiple browser tabs) are supported up to ``max_connections_per_user``.
    """

    def __init__(self, max_connections_per_user: int = 10):
        self.max_connections = max_connections_per_user
        # user_id -> {stream_id: Queue}
        self.user_connections: Dict[int, Dict[str, queue.Queue]] = defaultdict(dict)
        # event_type -> set of connected user_ids
        self.eventtype_connection_users: Dict[str, Set[int]] = defaultdict(set)
        # stream_id -> connection timestamp
        self.users_connect_time: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _generate_stream_id(self) -> str:
        return str(uuid.uuid4())

    def add_connection(self, user_id: int, stream: queue.Queue, event_type: str) -> str:
        with self._lock:
            user_conns = self.user_connections[user_id]
            if len(user_conns) >= self.max_connections:
                # Evict the oldest connection
                oldest_sid = min(
                    user_conns,
                    key=lambda sid: self.users_connect_time.get(sid, 0),
                )
                self._remove_connection_locked(user_id, oldest_sid, event_type)

            stream_id = self._generate_stream_id()
            self.user_connections[user_id][stream_id] = stream
            self.users_connect_time[stream_id] = time.time()
            self.eventtype_connection_users[event_type].add(user_id)
            return stream_id

    def _remove_connection_locked(self, user_id: int, stream_id: str, event_type: str):
        """Remove one connection; must be called *with* self._lock held."""
        user_conns = self.user_connections.get(user_id)
        if user_conns and stream_id in user_conns:
            del user_conns[stream_id]
        self.users_connect_time.pop(stream_id, None)
        if not self.user_connections.get(user_id):
            self.user_connections.pop(user_id, None)
            self.eventtype_connection_users[event_type].discard(user_id)

    def remove_connection(self, user_id: int, stream_id: str, event_type: str):
        with self._lock:
            self._remove_connection_locked(user_id, stream_id, event_type)

    def remove_all_connections(self, user_id: int):
        """Remove all connections belonging to ``user_id``.

        Bug fix: the original code tried ``stream_id.startswith(str(user_id))``
        to find connect-times to purge, but stream_id is a UUID and can never
        start with a numeric user_id.  We now track and delete the exact set of
        stream_ids we are removing.
        """
        with self._lock:
            if user_id not in self.user_connections:
                return
            stream_ids = list(self.user_connections[user_id].keys())
            del self.user_connections[user_id]
            for sid in stream_ids:
                self.users_connect_time.pop(sid, None)
            for event_type in self.eventtype_connection_users:
                self.eventtype_connection_users[event_type].discard(user_id)

    def get_user_streams(self, user_id: int) -> Set[queue.Queue]:
        with self._lock:
            return set(self.user_connections.get(user_id, {}).values())

    def get_all_streams(self) -> Set[queue.Queue]:
        with self._lock:
            all_streams: Set[queue.Queue] = set()
            for conns in self.user_connections.values():
                all_streams.update(conns.values())
            return all_streams

    def get_eventtype_users(self, event_type: str) -> Set[int]:
        with self._lock:
            return set(self.eventtype_connection_users.get(event_type, set()))


# ---------------------------------------------------------------------------
# EventManager
# ---------------------------------------------------------------------------

class EventManager:
    """Central SSE event queue, distribution, persistence, and recovery manager.

    Lifecycle
    ---------
    1. ``__init__`` -> clean up stale DB entries, start distributor + cleanup threads.
    2. ``create_event`` -> persist to DB; if user is online, enqueue for
       immediate delivery; otherwise the event waits in DB until reconnect.
    3. ``register_user_stream`` -> recover unread events from DB into the new stream.
    4. ``shutdown`` -> persist any queued-but-unsent events, then stop threads.
    """

    _event_classes: Dict[str, type[EventBase]] = {}

    def __init__(
        self,
        dbmgr: DbMgr,
        max_event_queue_size: int = 1000,
        max_events_per_stream: int = 100,
    ):
        self.mylogger = log.get_logger(self.__class__.__name__, level=logging.INFO)
        self.dbmgr = dbmgr
        self.connection_manager = ConnectionManager()
        self.event_queue: queue.Queue[EventBase] = queue.Queue(maxsize=max_event_queue_size)
        self.max_events_per_stream = max_events_per_stream
        self.lock = threading.Lock()
        self.is_shutting_down = False
        self.mylogger.info(
            f"EventManager.__init__ starting "
            f"(max_queue={max_event_queue_size}, max_per_stream={max_events_per_stream})"
        )
        self._recover_stored_events()
        self.distributor_thread = self._start_event_distributor()
        self.cleanup_thread = self._start_cleanup_scheduler()
        self.mylogger.info(
            f"EventManager.__init__ complete: "
            f"distributor_thread={self.distributor_thread.name}, "
            f"cleanup_thread={self.cleanup_thread.name}"
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @classmethod
    def register_event(cls, event_class: type[EventBase]):
        """Register an event class.  ``event_type`` is derived from the class
        name by stripping the trailing ``Event`` suffix."""
        event_type = event_class.__name__.removesuffix('Event')
        cls._event_classes[event_type] = event_class

    # ------------------------------------------------------------------
    # Event creation
    # ------------------------------------------------------------------

    def create_event(
        self,
        event_type: str,
        target_userid: int,
        priority: EventPriority = EventPriority.NORMAL,
        expire_after: int = None,           # minutes
        **payload_kwargs,
    ) -> EventBase:
        event_class = self._event_classes.get(event_type)
        if not event_class:
            raise ValueError(f"Unregistered event type: {event_type!r}")

        expired_at = (
            datetime.now(timezone.utc) + timedelta(minutes=expire_after)
            if expire_after else None
        )
        event = event_class(
            target_userid=target_userid,
            priority=priority,
            expired_at=expired_at,
            **payload_kwargs,
        )
        self._store_event(event)

        # Only enqueue for immediate delivery when the user is online
        if target_userid in self.connection_manager.user_connections:
            try:
                self._put_event(event)
            except queue.Full:
                self.mylogger.error(
                    f"Event queue full  event {event.id} for user {target_userid} dropped!"
                )
                event = None
        else:
            self.mylogger.debug(
                f"User {target_userid} offline; event {event.id} stored for later recovery."
            )
        return event

    def _put_event(self, event):
        with self.lock:
            self.event_queue.put(event)

    def send_raw_event(
        self,
        event_type: str,
        target_userid: int,
        payload: dict,
        priority: str = 'NORMAL',
    ) -> bool:
        """Send an ephemeral, non-persistent event to a connected user.

        Unlike ``create_event``, this method:
        - Does **not** require a registered EventBase subclass
        - Does **not** persist the event to the database
        - Is suitable for real-time push data (price ticks, live updates)

        Returns True if the event was enqueued, False if the user is offline.
        """
        if target_userid not in self.connection_manager.user_connections:
            return False
        raw = RawEventMessage(event_type, target_userid, payload, priority)
        try:
            self.event_queue.put_nowait(raw)
            return True
        except queue.Full:
            self.mylogger.warning(
                f"Event queue full — raw event type={event_type!r} for user={target_userid} dropped"
            )
            return False



    def _store_event(self, event: EventBase):
        with self.dbmgr.session_context() as session:
            entity = event.to_entity()
            if entity:
                session.add(entity)
                session.flush()          # assign DB id before commit
                event.id = entity.id
                session.commit()

    def set_event_read(self, event: EventBase):
        event.is_read = True
        with self.dbmgr.session_context() as session:
            entity = session.query(EventEntity).filter_by(id=event.id).one_or_none()
            if entity:
                entity.is_read = True

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def _recover_stored_events(self):
        """On startup, delete expired and already-read events from the DB."""
        with self.dbmgr.session_context() as session:
            stmt = select(EventEntity).where(
                (EventEntity.is_expired == True) | (EventEntity.is_read == True)
            )
            stale = session.execute(stmt).scalars().all()
            for entity in stale:
                session.delete(entity)
            if stale:
                session.commit()

    def _recover_user_events(self, user_id: int, event_type: str):
        """Push unread DB events for ``user_id`` into their newly opened stream."""
        with self.dbmgr.session_context() as session:
            stmt = (
                select(EventEntity)
                .where(
                    EventEntity.target_userid == user_id,
                    EventEntity.event_type == event_type,
                    EventEntity.is_read == False,
                )
                .order_by(EventEntity.priority.desc(), EventEntity.created_at.asc())
            )
            pending = session.execute(stmt).scalars().all()
            user_streams = self.connection_manager.get_user_streams(user_id)
            recovered = 0
            for entity in pending:
                try:
                    if entity.is_expired:
                        session.delete(entity)
                        continue
                    event_cls = self._event_classes.get(entity.event_type)
                    if not event_cls:
                        self.mylogger.warning(f"Unknown event type in DB: {entity.event_type!r}")
                        continue
                    event = event_cls.from_entity(entity)
                    if event:
                        event.is_recovered = True
                        recovered += 1
                        for stream in user_streams:
                            try:
                                stream.put_nowait(event) if stream.qsize() < self.max_events_per_stream \
                                    else (stream.get_nowait(), stream.put_nowait(event))
                            except queue.Full:
                                pass
                except Exception as exc:
                    self.mylogger.error(
                        f"Error recovering event {entity.id} for user {user_id}: {exc}"
                    )
            session.commit()
            if recovered:
                self.mylogger.debug(f"Recovered {recovered} events for user {user_id}.")

    # ------------------------------------------------------------------
    # Distribution
    # ------------------------------------------------------------------

    def _distribute_event(self, event: EventBase):
        streams = self.connection_manager.get_user_streams(event.target_userid)
        for stream in streams:
            try:
                if stream.qsize() < self.max_events_per_stream:
                    stream.put_nowait(event)
                else:
                    stream.get_nowait()
                    stream.put_nowait(event)
            except queue.Full:
                pass

    def _start_event_distributor(self) -> threading.Thread:
        def distributor():
            while not self.is_shutting_down:
                try:
                    while not self.event_queue.empty():
                        event: EventBase = self.event_queue.get(timeout=1)
                        if event.is_read or event.is_expired:
                            continue
                        self._distribute_event(event)
                except queue.Empty:
                    continue
                except Exception as exc:
                    self.mylogger.error(f"Event distribution error: {exc}")
        t = threading.Thread(name='sse_event_distributor', target=distributor, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    # Periodic cleanup
    # ------------------------------------------------------------------

    def clean_up_events(self):
        """Delete read or expired events from the DB.

        Bug fix: original code used Python ``or`` which evaluates the WHERE
        clause as a bool (always True).  Corrected to SQLAlchemy bitwise ``|``.
        """
        with self.dbmgr.session_context() as session:
            stmt = select(EventEntity).where(
                (EventEntity.is_read == True)
                | (EventEntity.expired_at <= datetime.now(timezone.utc))
            )
            to_delete = session.execute(stmt).scalars().all()
            for entity in to_delete:
                session.delete(entity)
            session.commit()

    def _start_cleanup_scheduler(self, interval_minutes: int = 30) -> threading.Thread:
        def scheduler():
            while not self.is_shutting_down:
                try:
                    self.clean_up_events()
                    time.sleep(interval_minutes * 60)
                except Exception as exc:
                    self.mylogger.error(f"Event cleanup error: {exc}")
        t = threading.Thread(name='sse_event_cleanup', target=scheduler, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    # Stream registration / deregistration
    # ------------------------------------------------------------------

    def register_user_stream(self, user_id: int, event_type: str) -> str | None:
        """Open a new SSE stream for ``user_id``.

        Returns the ``stream_id`` (UUID string) or ``None`` when the connection
        could not be established.  Use
        ``connection_manager.user_connections[user_id][stream_id]`` to get the
        actual ``queue.Queue`` for the streaming generator.
        """
        stream: queue.Queue[EventBase] = queue.Queue(maxsize=self.max_events_per_stream)
        stream_id = self.connection_manager.add_connection(user_id, stream, event_type)
        if stream_id:
            self._recover_user_events(user_id, event_type)
        return stream_id

    def unregister_user_stream(self, user_id: int, stream_id: str, event_type: str):
        self.connection_manager.remove_connection(user_id, stream_id, event_type)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        if self.is_shutting_down:
            self.mylogger.debug("shutdown() already in progress, skipping")
            return
        self.is_shutting_down = True
        
        import traceback
        caller_stack = ''.join(traceback.format_stack()[-5:-1])
        self.mylogger.info(
            f"[SHUTDOWN] EventManager.shutdown() called from:\n{caller_stack}"
        )
        
        self.mylogger.info("Shutting down SSE EventManager...")
        queued_events = self.event_queue.qsize()
        self.mylogger.info(f"  Queued events to persist: {queued_events}")
        
        # Persist any events still waiting in the in-memory queue
        while not self.event_queue.empty():
            try:
                event: EventBase = self.event_queue.get_nowait()
                if not event.is_read and not event.is_expired:
                    self._store_event(event)
            except queue.Empty:
                break
        
        # Disconnect all users
        disconnected_count = len(list(self.connection_manager.user_connections.keys()))
        self.mylogger.info(f"  Disconnecting {disconnected_count} connected users...")
        for uid in list(self.connection_manager.user_connections.keys()):
            self.connection_manager.remove_all_connections(uid)
        
        self.mylogger.info("All unprocessed events saved; connections closed.")
        
        self.mylogger.info(f"  Waiting for distributor_thread ({self.distributor_thread.name}) to stop...")
        self.distributor_thread.join(timeout=10)
        if self.distributor_thread.is_alive():
            self.mylogger.warning(f"  distributor_thread still alive after timeout (daemon)")
        else:
            self.mylogger.info(f"  distributor_thread stopped successfully")
        
        self.clean_up_events()
        
        self.mylogger.info(f"  Waiting for cleanup_thread ({self.cleanup_thread.name}) to stop...")
        self.cleanup_thread.join(timeout=10)
        if self.cleanup_thread.is_alive():
            self.mylogger.warning(f"  cleanup_thread still alive after timeout (daemon)")
        else:
            self.mylogger.info(f"  cleanup_thread stopped successfully")
        
        self.mylogger.info("SSE EventManager shutdown complete.")
