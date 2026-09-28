"""
SSEService  —  funlab-sse plugin

Install this package and the service activates automatically, replacing
``PollingNotificationProvider`` in funlab-flaskr with a full SSE back-end.

  Routes registered by this plugin:

    GET  /sse/<event_type>              SSE streaming endpoint

  （SSE-11：舊 docstring 曾宣稱的兩個測試／管理路由從未存在於本外掛，
  已刪除該幻覺描述。測試注入通知請直接在後端呼叫
  ``app.send_user_notification(...)``，見 docs/sse-plugin-development-guide。）

  The ``/notifications/*`` routes (poll / clear / dismiss) remain on the
  root blueprint registered by funlab-flaskr and automatically delegate to
  this service via ``current_app.notification_provider``.

  Public interface  (INotificationProvider):

    send_user_notification(title, message, target_userid, priority, expire_after)
    send_global_notification(title, message, priority, expire_after)
    fetch_unread(user_id) -> list[dict]
    dismiss_items(user_id, item_ids)
    dismiss_all(user_id)
    send_event(event_type, target_userid, payload, priority) -> bool
    get_connected_users(event_type) -> set
"""
from __future__ import annotations

import json
import logging
import queue

from flask import (
    Response,
    stream_with_context,
)
from flask_login import current_user
from funlab.core.notification import INotificationProvider
from funlab.core.plugin import ServicePlugin
from funlab.core.policy import is_authenticated_user

from .manager import EventManager, STREAM_CLOSED
from .model import EventBase, EventEntity, EventPriority, SystemNotificationEvent


class SSEService(ServicePlugin, INotificationProvider):
    """SSE plugin that can act as a drop-in replacement for funlab-flaskr's
    built-in SSE implementation."""

    default_route_policy = is_authenticated_user

    def __init__(self, app):
        super().__init__(app)
        self._setup(app)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup(self, app):
        EventManager.register_event(SystemNotificationEvent)
        # SSE is self-contained: create only the event table.
        # No dependency on funlab-auth or APP_ENTITIES_REGISTRY because
        # EventEntity no longer carries an ORM-level FK to user.id.
        EventEntity.__table__.create(bind=app.dbmgr.get_db_engine(), checkfirst=True)
        self.sse_mgr = EventManager(app.dbmgr)

        # Register SSE routes on our own blueprint
        self._register_sse_routes()

        # IMPORTANT: SSE is a daemon service that should persist across ALL requests.
        # We DO NOT register teardown_appcontext() here, as that would shutdown
        # the service after every single request.
        # Instead, SSE is properly shut down via unload() -> stop() -> _on_stop()
        # when the Flask app itself shuts down (managed by plugin_manager.cleanup()).

        # Register this service as the app's notification provider.
        # SSE-specific routes will be registered when FunlabFlask calls
        # app.notification_provider.register_routes(blueprint).
        #
        # All calls to app.send_user_notification / send_global_notification and
        # the /notifications/* HTTP routes will now delegate to SSEService.
        app.set_notification_provider(self)
        app.mylogger.info(
            "SSEService activated: replaced PollingNotificationProvider."
        )

        # SSE-10◆Q6：多 worker 護欄（只警示、不影響啟動）
        self._warn_if_multiworker(app)

    @staticmethod
    def _warn_if_multiworker(app) -> None:
        """SSE-10◆Q6：WSGI 多 worker 啟動護欄。

        SSE 的連線表是 per-process in-memory；只有單進程部署正確。
        PM 裁示（2026-09-28）正式環境續用 waitress（單進程）滿足前提。
        若切 gunicorn 且 gunicorn_conf.workers>1，事件只送達「恰好同 worker」
        的連線——即時性靜默失效，故於啟動時明語警示。
        護欄自身 try/except 包死，不可影響啟動。
        """
        try:
            wsgi = str(app.config.get('WSGI', 'flask')).lower()
            if wsgi == 'gunicorn':
                from funlab.flaskr.conf import gunicorn_conf
                import multiprocessing
                workers = getattr(gunicorn_conf, 'workers', 1) or 1
                if workers < 1:
                    workers = multiprocessing.cpu_count() * 2 + 1
                if workers > 1:
                    app.mylogger.warning(
                        f"SSEService: 偵測到 WSGI=gunicorn 且 workers={workers}。"
                        "SSE 連線表為 per-process in-memory，多 worker 下即時推送會靜默漏送"
                        "（僅剩 /notifications/poll 回補）。請改單 worker（waitress 或 "
                        "gunicorn -w 1 --threads N）或實作跨進程廣播後再切換。"
                    )
        except Exception as guard_exc:   # 護欄本身不可影響啟動
            app.mylogger.debug(f"SSE multi-worker guard skipped: {guard_exc}")

    def _teardown(self, _exception):
        """Teardown callback invoked by Flask at the end of request context.

        NOTE: This is called once per request, NOT once at application shutdown.
        We do NOT shut down the EventManager here because it's a daemon service
        that should persist across multiple requests.

        The proper shutdown happens via unload() -> stop() -> _on_stop() when
        the plugin manager cleans up at Flask application shutdown.
        """
        pass  # Do NOT shutdown SSE here - this is per-request cleanup only

    # ------------------------------------------------------------------
    # Public notification helpers (mirror FunlabFlask methods)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_priority(priority) -> EventPriority:
        """Accept both plain strings ('NORMAL', 'HIGH') and EventPriority enums."""
        if isinstance(priority, EventPriority):
            return priority
        try:
            return EventPriority[str(priority).upper()]
        except KeyError:
            return EventPriority.NORMAL

    def send_event(
        self,
        event_type: str,
        target_userid: int,
        payload: dict,
        priority: str = 'NORMAL',
        expire_after: int = None,
    ) -> bool:
        """Send an ephemeral, non-persistent event to a connected user.

        Designed for plugins that want to push real-time data without depending
        on EventBase / EventPriority.  Events sent here are **not** persisted to
        the database; suitable for live ticks and transient updates.

        Returns True if the event was enqueued for the online user, False otherwise.
        """
        if self.sse_mgr is None:
            self.app.mylogger.warning(
                f"SSEService.send_event ignored: sse_mgr is not running "
                f"(event_type={event_type!r}, target_userid={target_userid})"
            )
            return False
        return self.sse_mgr.send_raw_event(
            event_type=event_type,
            target_userid=target_userid,
            payload=payload,
            priority=str(priority).upper(),
        )

    def send_user_notification(
        self,
        title: str,
        message: str,
        target_userid: int,
        priority: 'str | EventPriority' = 'NORMAL',
        expire_after: int = None,
    ) -> EventBase | None:
        """Send a SystemNotification event to *target_userid* (persisted to DB).

        SSE-12◆Q7（PM 裁示 2026-09-28）：``target_userid`` 為必填——None 於入口
        顯式拒絕（guard log＋回傳 None，不寫孤列、不廣播）。要廣播請改用
        :meth:`send_global_notification`。funlab-libs 介面文件舊述
        「None＝全域」已過時，待 libs 側同步更正。
        """
        if target_userid is None:
            self.app.mylogger.warning(
                f"SSEService.send_user_notification REJECTED: target_userid 必填，"
                f"拒絕寫入 None 孤列 (title={title!r})；廣播請用 send_global_notification"
            )
            return None
        if self.sse_mgr is None:
            self.app.mylogger.warning(
                f"SSEService.send_user_notification ignored: sse_mgr is not running "
                f"(title={title!r}, target_userid={target_userid})"
            )
            return None
        return self.sse_mgr.create_event(
            event_type='SystemNotification',
            target_userid=target_userid,
            priority=self._normalize_priority(priority),
            expire_after=expire_after,
            title=title,
            message=message,
        )

    def send_global_notification(
        self,
        title: str,
        message: str,
        priority: 'str | EventPriority' = 'NORMAL',
        expire_after: int = None,
    ):
        """Broadcast a SystemNotification event to all currently-connected users."""
        if self.sse_mgr is None:
            self.app.mylogger.warning(
                f"SSEService.send_global_notification ignored: sse_mgr is not running "
                f"(title={title!r})"
            )
            return
        online_users = self.sse_mgr.connection_manager.get_eventtype_users('SystemNotification')
        for uid in online_users:
            self.sse_mgr.create_event(
                event_type='SystemNotification',
                target_userid=uid,
                priority=self._normalize_priority(priority),
                expire_after=expire_after,
                title=title,
                message=message,
            )

    def get_connected_users(self, event_type: str) -> set:
        """Return the set of user_ids currently subscribed to *event_type*."""
        return self.sse_mgr.connection_manager.get_eventtype_users(event_type)

    def fetch_unread(self, user_id: int) -> list[dict]:
        """Return all unread / unexpired DB events for *user_id* as dicts.

        Used by the ``/notifications/poll`` fallback endpoint when SSE is active
        (e.g. after a page reload before the SSE stream reconnects).
        """
        from sqlalchemy import select
        with self.app.dbmgr.session_context() as session:
            stmt = (
                select(EventEntity)
                .where(
                    EventEntity.target_userid == user_id,
                    EventEntity.is_read == False,
                )
                .order_by(EventEntity.created_at.asc())
            )
            entities = session.execute(stmt).scalars().all()
            result = []
            for entity in entities:
                if entity.is_expired:
                    continue
                event_cls = self.sse_mgr._event_classes.get(entity.event_type)
                if not event_cls:
                    continue
                event = event_cls.from_entity(entity)
                if event:
                    d = event.to_dict()
                    d['is_recovered'] = True  # delivered via poll; not a fresh SSE push
                    result.append(d)
            return result

    def dismiss_items(self, user_id: int, item_ids: list[int]) -> None:
        """Mark specific events as read in the DB for *user_id*.

        SSE-06：不顯式 commit——由 session_context 最外層提交（巢狀安全）。
        """
        with self.app.dbmgr.session_context() as session:
            session.query(EventEntity).filter(
                EventEntity.id.in_(item_ids),
                EventEntity.target_userid == user_id,
            ).update({'is_read': True}, synchronize_session=False)

    def dismiss_all(self, user_id: int) -> None:
        """Mark all unread events as read in the DB for *user_id*.

        SSE-06：不顯式 commit——由 session_context 最外層提交（巢狀安全）。
        """
        with self.app.dbmgr.session_context() as session:
            session.query(EventEntity).filter(
                EventEntity.target_userid == user_id,
                EventEntity.is_read == False,
            ).update({'is_read': True}, synchronize_session=False)

    @property
    def supports_realtime(self) -> bool:
        return True

    @property
    def metrics(self):
        base_metrics = super().metrics
        connection_manager = self.sse_mgr.connection_manager if self.sse_mgr else None
        if connection_manager is None:
            base_metrics.update({
                'connected_users': 0,
                'connected_streams': 0,
                'event_queue_size': 0,
            })
            return base_metrics

        connected_users = len(connection_manager.user_connections)
        connected_streams = sum(len(streams) for streams in connection_manager.user_connections.values())
        event_queue_size = self.sse_mgr.event_queue.qsize() if self.sse_mgr and self.sse_mgr.event_queue else 0
        base_metrics.update({
            'connected_users': connected_users,
            'connected_streams': connected_streams,
            'event_queue_size': event_queue_size,
            'dropped_event_count': getattr(self.sse_mgr, 'dropped_event_count', 0),  # SSE-01
        })
        return base_metrics

    def _perform_health_check(self) -> bool:
        if self.sse_mgr is None:
            return False
        if getattr(self.app, 'notification_provider', None) is not self:
            return False
        return True

    # ------------------------------------------------------------------
    # ServicePlugin lifecycle overrides
    # ------------------------------------------------------------------

    def _on_start(self):
        """Called when the plugin is started (via start())."""
        if self.sse_mgr:
            self.app.mylogger.debug(f"{self.name}: _on_start()")

    def _on_stop(self):
        """Called when the plugin is stopped (via stop()).

        Gracefully shuts down all SSE resources.
        """
        if self.sse_mgr:
            self.app.mylogger.debug(f"{self.name}: _on_stop() - shutting down EventManager")
            self.sse_mgr.shutdown()
            self.sse_mgr = None
            self.app.mylogger.debug(f"{self.name}: _on_stop() complete")

    def _on_reload(self):
        """Reload SSE service configuration and rebuild runtime manager.

        Keep route registration unchanged (Blueprint is immutable after registration).
        """
        super()._on_reload()
        self.sse_mgr = EventManager(self.app.dbmgr)
        self.app.set_notification_provider(self)

    def unload(self):
        """Called by plugin manager when the Flask app shuts down.

        Delegates to stop() so the full Enhanced lifecycle is honoured,
        ensuring _on_stop() / EventManager.shutdown() runs exactly once.
        """
        self.app.mylogger.debug(f"{self.name}: unload() - plugin shutdown initiated")
        self.stop()
        self.app.mylogger.debug(f"{self.name}: unload() complete")

    # ------------------------------------------------------------------
    # Route registration (INotificationProvider.register_routes implementation)
    # ------------------------------------------------------------------

    def _register_sse_routes(self) -> None:
        """Register SSE streaming routes on this plugin's own blueprint.

        This is called during _setup() to ensure routes are registered before
        the blueprint is registered with Flask.
        """
        # self.app.mylogger.debug(f"[SSEService] Registering SSE routes on own blueprint: {self.blueprint.name}")

        # Register on own blueprint (sse_bp), which has url_prefix='/sse'
        # So /SystemNotification becomes /sse/SystemNotification
        @self.blueprint.route('/<event_type>')
        def stream_events(event_type):
            user_id = current_user.id
            stream_id = self.sse_mgr.register_user_stream(user_id, event_type)
            if not stream_id:
                return Response("Max connections reached.", status=429)

            def event_stream():
                try:
                    user_stream = (
                        self.sse_mgr.connection_manager
                        .user_connections.get(user_id, {})
                        .get(stream_id)
                    )
                    if not user_stream:
                        return
                    while True:
                        try:
                            event = user_stream.get(timeout=10)
                            if event is STREAM_CLOSED:
                                # SSE-05：本連線已被淘汰（超過 max_connections）→ 體面退出
                                return
                            sse = (
                                f"event: {event.event_type}\n"
                                f"data: {json.dumps(event.to_dict())}\n\n"
                            )
                            yield sse
                        except queue.Empty:
                            yield 'event: heartbeat\ndata: {"status":"heartbeat"}\n\n'
                except GeneratorExit:
                    pass
                except Exception as exc:
                    logging.error(
                        f"SSE stream error user={user_id} stream={stream_id}: {exc}"
                    )
                finally:
                    mgr = self.sse_mgr
                    if mgr is not None:                 # SSE-13：shutdown 競態保護
                        mgr.unregister_user_stream(user_id, stream_id, event_type)

            return Response(
                stream_with_context(event_stream()),
                content_type='text/event-stream',
            )

    def register_routes(self, blueprint) -> None:
        """Register SSE-specific routes.

        This implements :meth:`~funlab.core.notification.INotificationProvider.register_routes`.

        **Note:** SSE routes are now registered directly on the SSE plugin's own blueprint
        in `_register_sse_routes()` during `_setup()`. This method is kept for interface
        compatibility but is essentially a no-op since routes are already registered.

        Event dismissal is unified via the generic ``POST /notifications/dismiss`` endpoint
        handled by FunlabFlask.
        """
        # Routes already registered on own blueprint during _setup()
        self.app.mylogger.debug(
            f"[SSEService] register_routes called (no-op: routes already registered on {self.blueprint.name})"
        )
        pass

