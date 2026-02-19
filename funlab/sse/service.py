"""
SSEService    funlab-sse plugin

When ``SSE_PROVIDER = 'plugin'`` is set in config.toml this service takes over
all SSE responsibilities from funlab-flaskr's built-in implementation:

  Routes registered (same URLs as the built-in implementation so the frontend
  and templates remain unchanged):

    GET  /sse/<event_type>              SSE streaming endpoint
    POST /mark_event_read/<event_id>    Mark a single event as read
    POST /mark_events_read              Bulk mark-as-read
    POST /generate_notification         Test / programmatic event injection
    GET  /ssetest                       (Admin) SSE test page

  Public helper methods on the service instance:

    send_user_system_notification(...)
    send_all_users_system_notification(...)

When ``SSE_PROVIDER = 'builtin'`` (the default) this service loads but stays
entirely passive  no routes, no EventManager  so the built-in code continues
to operate without interference.
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
from funlab.core.plugin import ServicePlugin

from .manager import EventManager
from .model import EventBase, EventEntity, EventPriority, SystemNotificationEvent


class SSEService(ServicePlugin):
    """SSE plugin that can act as a drop-in replacement for funlab-flaskr's
    built-in SSE implementation."""

    def __init__(self, app):
        super().__init__(app)
        self._provider = app.config.get('SSE_PROVIDER', 'builtin')
        if self._is_active:
            self._setup(app)

    @property
    def _is_active(self) -> bool:
        return self._provider == 'plugin'

    # ------------------------------------------------------------------
    # Setup (only when SSE_PROVIDER = 'plugin')
    # ------------------------------------------------------------------

    def _setup(self, app):
        EventManager.register_event(SystemNotificationEvent)
        # SSE is self-contained: create only the event table.
        # No dependency on funlab-auth or APP_ENTITIES_REGISTRY because
        # EventEntity no longer carries an ORM-level FK to user.id.
        EventEntity.__table__.create(bind=app.dbmgr.get_db_engine(), checkfirst=True)
        self.sse_mgr = EventManager(app.dbmgr)
        app.teardown_appcontext(self._teardown)
        self._register_routes()
        # Expose this service on the app for use by FunlabFlask helper methods
        app.sse_service = self
        app.mylogger.info("SSEService activated as SSE_PROVIDER='plugin'.")

    def _teardown(self, _exception):
        if self._is_active and self.sse_mgr:
            self.sse_mgr.shutdown()

    # ------------------------------------------------------------------
    # Public notification helpers (mirror FunlabFlask methods)
    # ------------------------------------------------------------------

    def send_user_system_notification(
        self,
        title: str,
        message: str,
        target_userid: int = None,
        priority: EventPriority = EventPriority.NORMAL,
        expire_after: int = None,
    ) -> EventBase | None:
        if not self._is_active:
            return None
        return self.sse_mgr.create_event(
            event_type='SystemNotification',
            target_userid=target_userid,
            priority=priority,
            expire_after=expire_after,
            title=title,
            message=message,
        )

    def send_all_users_system_notification(
        self,
        title: str,
        message: str,
        priority: EventPriority = EventPriority.NORMAL,
        expire_after: int = None,
    ):
        if not self._is_active:
            return
        online_users = self.sse_mgr.connection_manager.get_eventtype_users('SystemNotification')
        for uid in online_users:
            self.sse_mgr.create_event(
                event_type='SystemNotification',
                target_userid=uid,
                priority=priority,
                expire_after=expire_after,
                title=title,
                message=message,
            )

    # ------------------------------------------------------------------
    # ServicePlugin lifecycle stubs
    # ------------------------------------------------------------------

    def start_service(self): pass
    def stop_service(self): pass
    def restart_service(self): self.stop_service(); self.start_service()
    def reload_service(self): pass

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self):
        """Add SSE routes to the plugin blueprint.

        The blueprint is registered with ``url_prefix=''`` so the paths match
        the built-in implementation exactly.
        """
        # Override the default blueprint prefix to mount at root
        self.blueprint.url_prefix = ''

        @self.blueprint.route('/sse/<event_type>')
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

        @self.blueprint.route('/mark_event_read/<int:event_id>', methods=['POST'])
        @login_required
        def mark_event_read(event_id):
            try:
                from .model import EventEntity
                with self.app.dbmgr.session_context() as session:
                    entity = session.query(EventEntity).filter_by(
                        id=event_id,
                        target_userid=current_user.id,
                    ).first()
                    if not entity:
                        return jsonify({"status": "error", "message": "Not found or access denied"}), 404
                    if entity.is_read:
                        return jsonify({"status": "warning", "message": "Already read"}), 200
                    entity.is_read = True
                    session.commit()
                return jsonify({"status": "success", "message": "Event marked as read"}), 200
            except Exception as exc:
                logging.error(f"mark_event_read error: {exc}")
                return jsonify({"status": "error", "message": "Internal server error"}), 500

        @self.blueprint.route('/mark_events_read', methods=['POST'])
        @login_required
        def mark_events_read():
            data = request.get_json()
            event_ids = data.get('event_ids') if data else None
            if not event_ids or not isinstance(event_ids, list):
                return jsonify({"status": "error", "message": "Invalid or missing event_ids"}), 400
            try:
                from .model import EventEntity
                with self.app.dbmgr.session_context() as session:
                    updated = session.query(EventEntity).filter(
                        EventEntity.id.in_(event_ids),
                        EventEntity.target_userid == current_user.id,
                        EventEntity.is_read == False,
                    ).update({'is_read': True}, synchronize_session=False)
                    session.commit()
                return jsonify({"status": "success", "message": f"{updated} events marked as read"}), 200
            except Exception as exc:
                logging.error(f"mark_events_read error: {exc}")
                return jsonify({"status": "error", "message": "Internal server error"}), 500

        @self.blueprint.route('/generate_notification', methods=['POST'])
        @login_required
        def generate_notification():
            title = request.form.get('title', 'Test Notification')
            message = request.form.get('message', 'This is a test notification.')
            target_user = request.form.get('target_userid', None)
            target_userid = int(target_user) if target_user else current_user.id
            priority_level = request.form.get('priority', 'NORMAL')
            priority = (
                EventPriority[priority_level]
                if priority_level in EventPriority.__members__
                else EventPriority.NORMAL
            )
            expire_after = request.form.get('expire_after', 5, type=int)
            event = self.send_user_system_notification(
                title=title,
                message=message,
                target_userid=target_userid,
                priority=priority,
                expire_after=expire_after,
            )
            if event:
                return jsonify({
                    "status": "success",
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "created_at": event.created_at.isoformat(),
                }), 201
            return jsonify({"status": "error", "message": "Failed to create notification"}), 500

        @self.blueprint.route('/ssetest')
        @login_required
        @admin_required
        def ssetest():
            return render_template('ssetest.html')
