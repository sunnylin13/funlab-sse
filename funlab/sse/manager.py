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

    必須與 distributor 的欄位契約相容：distributor 會讀
    ``is_read`` / ``is_expired`` / ``target_userid``（見 _start_event_distributor、
    _distribute_event）。少了任何一個，事件會在分發時拋 AttributeError 被丢棄。
    """

    __slots__ = ('event_type', 'target_userid', '_data')

    #: 即時事件永遠視為未讀（無持久化、無已讀追蹤）
    is_read = False

    def __init__(self, event_type: str, target_userid: int, payload: dict, priority: str = 'NORMAL'):
        self.event_type = event_type
        self.target_userid = target_userid
        self._data = {
            'id': None,
            'event_type': event_type,
            'priority': priority.upper(),
            'target_userid': target_userid,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'payload': payload,
            'is_recovered': False,
        }

    @property
    def is_expired(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return self._data


# ---------------------------------------------------------------------------
# Stream-closed sentinel (SSE-05)
# ---------------------------------------------------------------------------

class _StreamClosed:
    """Sentinel：放進被淘汰連線的 queue，generator 見到即退出。"""
    __slots__ = ()

    def __repr__(self):            # pragma: no cover
        return '<STREAM_CLOSED>'


STREAM_CLOSED = _StreamClosed()


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Manages per-user SSE stream connections.

    Each connection is identified by a UUID ``stream_id`` and assigned to a
    specific ``event_type``.  Multiple concurrent connections per user
    (e.g. multiple browser tabs) are supported up to ``max_connections_per_user``.

    投遞契約（SSE-04）：stream 只收「自己 event_type」的事件
    （get_user_streams 過濾）。
    eviction 時對被淘汰的 queue 放入 STREAM_CLOSED sentinel（SSE-05），
    讓該連線的 streaming generator 能體面退出（避免幽靈連線續命）。
    """

    def __init__(self, max_connections_per_user: int = 10):
        self.max_connections = max_connections_per_user
        # user_id -> {stream_id: Queue}
        self.user_connections: Dict[int, Dict[str, queue.Queue]] = defaultdict(dict)
        # event_type -> set of connected user_ids
        self.eventtype_connection_users: Dict[str, Set[int]] = defaultdict(set)
        # stream_id -> connection timestamp
        self.users_connect_time: Dict[str, float] = {}
        # stream_id -> event_type（SSE-04：投遞時據以過濾）
        self.stream_event_type: Dict[str, str] = {}
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
                evicted_stream = user_conns.get(oldest_sid)
                self._remove_connection_locked(
                    user_id, oldest_sid, self.stream_event_type.get(oldest_sid, event_type)
                )
                if evicted_stream is not None:
                    self._signal_close(evicted_stream)   # SSE-05

            stream_id = self._generate_stream_id()
            self.user_connections[user_id][stream_id] = stream
            self.stream_event_type[stream_id] = event_type
            self.users_connect_time[stream_id] = time.time()
            self.eventtype_connection_users[event_type].add(user_id)
            return stream_id

    @staticmethod
    def _signal_close(stream: queue.Queue):
        """SSE-05：通知被淘汰連線的 generator 終止（不阻塞、可能滿則先腾一格）。"""
        try:
            stream.put_nowait(STREAM_CLOSED)
        except queue.Full:
            try:
                stream.get_nowait()
                stream.put_nowait(STREAM_CLOSED)
            except queue.Empty:      # pragma: no cover - race
                pass

    def _remove_connection_locked(self, user_id: int, stream_id: str, event_type: str):
        """Remove one connection; must be called *with* self._lock held."""
        user_conns = self.user_connections.get(user_id)
        if user_conns and stream_id in user_conns:
            del user_conns[stream_id]
        self.users_connect_time.pop(stream_id, None)
        self.stream_event_type.pop(stream_id, None)
        if not self.user_connections.get(user_id):
            self.user_connections.pop(user_id, None)
            users = self.eventtype_connection_users.get(event_type)
            if users is not None:
                users.discard(user_id)
                if not users:                     # 探針 E：空集合垃圾鍵即時回收
                    self.eventtype_connection_users.pop(event_type, None)

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
                self.stream_event_type.pop(sid, None)
            for event_type in list(self.eventtype_connection_users.keys()):
                users = self.eventtype_connection_users[event_type]
                users.discard(user_id)
                if not users:
                    self.eventtype_connection_users.pop(event_type, None)

    def get_user_streams(self, user_id: int, event_type: str = None) -> Set[queue.Queue]:
        """回傳使用者連線。給定 event_type 時只回傳訂閱該型別的連線。"""
        with self._lock:
            conns = self.user_connections.get(user_id, {})
            if event_type is None:
                return set(conns.values())
            return {
                stream for sid, stream in conns.items()
                if self.stream_event_type.get(sid) == event_type
            }

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
        self.lock = threading.Lock()          # 保留：給未來需要跨物件原子操作的使用
        self.dropped_event_count = 0          # SSE-01：佇列滿時被丢棄的持久事件累計數（observability）
        self.is_shutting_down = False
        self.mylogger.debug(
            f"EventManager.__init__ starting "
            f"(max_queue={max_event_queue_size}, max_per_stream={max_events_per_stream})"
        )
        self._recover_stored_events()
        self.distributor_thread = self._start_event_distributor()
        self.cleanup_thread = self._start_cleanup_scheduler()
        self.mylogger.debug(
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
            if not self._put_event(event):
                self.mylogger.error(
                    f"Event queue full  event {event.id} for user {target_userid} "
                    f"not enqueued; will recover from DB on next stream."
                )
                event = None
        else:
            self.mylogger.debug(
                f"User {target_userid} offline; event {event.id} stored for later recovery."
            )
        return event

    def _put_event(self, event) -> bool:
        """SSE-01：非阻塞放入主事件佇列。Queue 本身 thread-safe，不需要外層鎖。

        佇列滿時丢棄並回傳 False（持久化已在 _store_event 完成，
        使用者上線後仍可經 _recover_user_events 由 DB 回補，不丢資料）。
        """
        try:
            self.event_queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped_event_count += 1
            self.mylogger.warning(
                f"Event queue full; event {getattr(event, 'id', None)} "
                f"for user {getattr(event, 'target_userid', None)} deferred to DB recovery "
                f"(dropped_total={self.dropped_event_count})"
            )
            return False

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
        # SSE-06：不顯式 commit——巢狀語意（funlab-libs LIB-01）下由最外層
        # session_context 負責 commit；顯式 commit 會把外層未提交交易提前提交。
        # flush 仍保留：取 DB 端 id（在交易內發出 SQL，不提交）。
        with self.dbmgr.session_context() as session:
            entity = event.to_entity()
            if entity:
                session.add(entity)
                session.flush()          # assign DB id before commit
                event.id = entity.id

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
        """On startup, delete expired and already-read events from the DB.

        SSE-06：不自顯式 commit（巢狀安全，見 _store_event 註解）。
        """
        with self.dbmgr.session_context() as session:
            stmt = select(EventEntity).where(
                (EventEntity.is_expired == True) | (EventEntity.is_read == True)
            )
            stale = session.execute(stmt).scalars().all()
            for entity in stale:
                session.delete(entity)

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
            # SSE-04：只回補該 event_type 的連線（DB 查詢本就按型別，投遞不再跨型別誤送）
            user_streams = self.connection_manager.get_user_streams(user_id, event_type=event_type)
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
            # SSE-06：不顯式 commit（巢狀安全，見 _store_event 註解）
            if recovered:
                self.mylogger.debug(f"Recovered {recovered} events for user {user_id}.")

    # ------------------------------------------------------------------
    # Distribution
    # ------------------------------------------------------------------

    def _distribute_event(self, event: EventBase):
        # SSE-04：只投給訂閱該 event_type 的連線
        streams = self.connection_manager.get_user_streams(
            event.target_userid, event_type=event.event_type
        )
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
            # Blocking get(timeout=1): sleeps when the queue is empty instead
            # of busy-waiting on empty(), which pinned a core at 100% CPU.
            while not self.is_shutting_down:
                try:
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

        SSE-06：不顯式 commit（巢狀安全，見 _store_event 註解）。
        """
        with self.dbmgr.session_context() as session:
            stmt = select(EventEntity).where(
                (EventEntity.is_read == True)
                | (EventEntity.expired_at <= datetime.now(timezone.utc))
            )
            to_delete = session.execute(stmt).scalars().all()
            for entity in to_delete:
                session.delete(entity)

    def _start_cleanup_scheduler(self, interval_minutes: int = 30) -> threading.Thread:
        def scheduler():
            while not self.is_shutting_down:
                try:
                    self.clean_up_events()
                except Exception as exc:
                    self.mylogger.error(f"Event cleanup error: {exc}")
                finally:
                    # SSE-09：無論好壞都要睡，錯誤時不形成緊迫重試迴圈（同 PR#1 教訓）
                    time.sleep(interval_minutes * 60)
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
