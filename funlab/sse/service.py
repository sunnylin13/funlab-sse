"""
SSEService  —  funlab-sse plugin

Install this package and the service activates automatically, replacing
``PollingNotificationProvider`` in funlab-flaskr with a full SSE back-end.

  Routes registered by this plugin:

    GET  /sse/<event_type>              SSE streaming endpoint
    POST /generate_notification         Test / programmatic event injection
    GET  /ssetest                       (Admin) SSE test page

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
import traceback

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    stream_with_context,
)
from flask_login import current_user, login_required
from funlab.core.auth import admin_required
from funlab.core.notification import INotificationProvider
from funlab.core.enhanced_plugin import EnhancedServicePlugin

from .manager import EventManager
from .model import EventBase, EventEntity, EventPriority, SystemNotificationEvent


class SSEService(EnhancedServicePlugin, INotificationProvider):
    """SSE plugin that can act as a drop-in replacement for funlab-flaskr's
    built-in SSE implementation."""

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
        # Instead, SSE is properly shut down via unload() → stop() → _on_stop()
        # when the Flask app itself shuts down (managed by plugin_manager.cleanup()).

        # Register this service as the app's notification provider.
        # SSE-specific routes will be registered when FunlabFlask calls
        # app.notification_provider.register_routes(blueprint).
        #
        # All calls to app.send_user_notification / send_global_notification and
        # the /notifications/* HTTP routes will now delegate to SSEService.
        app.set_notification_provider(self)
        app.mylogger.info(
            "SSEService activated: replaced PollingNotificationProvider. "
            "SSE will shutdown when Flask app exits (via plugin lifecycle management)."
        )

    def _teardown(self, _exception):
        """Teardown callback invoked by Flask at the end of request context.

        NOTE: This is called once per request, NOT once at application shutdown.
        We do NOT shut down the EventManager here because it's a daemon service
        that should persist across multiple requests.

        The proper shutdown happens via unload() → stop() → _on_stop() when
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
        target_userid: int = None,
        priority: 'str | EventPriority' = 'NORMAL',
        expire_after: int = None,
    ) -> EventBase | None:
        """Send a SystemNotification event to *target_userid* (persisted to DB)."""
        return self.sse_mgr.create_event(
            event_type='SystemNotification',
            target_userid=target_userid,
            priority=self._normalize_priority(priority),
            expire_after=expire_after,
            title=title,
            message=message,
        )

    # Backward-compatibility alias
    send_user_system_notification = send_user_notification

    def send_global_notification(
        self,
        title: str,
        message: str,
        priority: 'str | EventPriority' = 'NORMAL',
        expire_after: int = None,
    ):
        """Broadcast a SystemNotification event to all currently-connected users."""
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

    # Backward-compatibility alias
    send_all_users_system_notification = send_global_notification

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
                    d['is_recovered'] = True  # delivered via poll ≠ fresh SSE push
                    result.append(d)
            return result

    def dismiss_items(self, user_id: int, item_ids: list[int]) -> None:
        """Mark specific events as read in the DB for *user_id*."""
        with self.app.dbmgr.session_context() as session:
            session.query(EventEntity).filter(
                EventEntity.id.in_(item_ids),
                EventEntity.target_userid == user_id,
            ).update({'is_read': True}, synchronize_session=False)
            session.commit()

    def dismiss_all(self, user_id: int) -> None:
        """Mark all unread events as read in the DB for *user_id*."""
        with self.app.dbmgr.session_context() as session:
            session.query(EventEntity).filter(
                EventEntity.target_userid == user_id,
                EventEntity.is_read == False,
            ).update({'is_read': True}, synchronize_session=False)
            session.commit()

    @property
    def supports_realtime(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # ServicePlugin lifecycle overrides
    # ------------------------------------------------------------------

    def _on_start(self):
        """Called when the plugin is started (via start())."""
        if self.sse_mgr:
            self.app.mylogger.info(f"{self.name}: _on_start()")

    def _on_stop(self):
        """Called when the plugin is stopped (via stop()).

        Gracefully shuts down all SSE resources.
        """
        if self.sse_mgr:
            self.app.mylogger.info(f"{self.name}: _on_stop() - shutting down EventManager")
            self.sse_mgr.shutdown()
            self.sse_mgr = None
            self.app.mylogger.info(f"{self.name}: _on_stop() complete")

    def reload(self):
        """Reload service configuration (no-op for SSE)."""
        pass

    def unload(self):
        """Called by plugin manager when the Flask app shuts down.

        Delegates to stop() so the full Enhanced lifecycle is honoured,
        ensuring _on_stop() / EventManager.shutdown() runs exactly once.
        """
        self.app.mylogger.info(f"{self.name}: unload() - plugin shutdown initiated")
        self.stop()
        self.app.mylogger.info(f"{self.name}: unload() complete")

    # ------------------------------------------------------------------
    # Route registration (INotificationProvider.register_routes implementation)
    # ------------------------------------------------------------------

    def _register_sse_routes(self) -> None:
        """Register SSE streaming routes on this plugin's own blueprint.

        This is called during _setup() to ensure routes are registered before
        the blueprint is registered with Flask.
        """
        self.app.mylogger.info(f"[SSEService] Registering SSE routes on own blueprint: {self.blueprint.name}")

        # Register on own blueprint (sse_bp), which has url_prefix='/sse'
        # So /SystemNotification becomes /sse/SystemNotification
        @self.blueprint.route('/<event_type>')
        @login_required
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
                            event: EventBase = user_stream.get(timeout=10)
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
                    self.sse_mgr.unregister_user_stream(user_id, stream_id, event_type)

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

