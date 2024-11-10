from datetime import datetime, timedelta, timezone
import json
import time

from flask import (Response, flash, g, jsonify, redirect, render_template, request, stream_with_context,
                   url_for, current_app)
from flask_login import current_user, login_required
from funlab.core.menu import Menu, MenuItem
from funlab.core.plugin import ServicePlugin, ViewPlugin
from sqlalchemy import and_, or_, select, update

from flask_restx import Api, Resource, Namespace

from funlab.flaskr.app import FunlabFlask
from .model import EventBase, EventEntity, EventPriority, PayloadBase
from .manager import EventManager

class SSEService(ServicePlugin):
    _event_classes: dict[str, type[EventBase]] = {}

    @classmethod
    def register_event(cls, event_type: str, event_class: type[EventBase]):
        cls._event_classes[event_type] = event_class

    @classmethod
    def create_event(cls, event_type: str, payload: PayloadBase,
                    target_userid: int = None, priority: EventPriority = EventPriority.NORMAL, is_read: bool = False,
                    created_at: datetime = datetime.now(timezone.utc), expires_at: datetime = None, **kwargs) -> EventBase:
        if event_type not in cls._event_classes:
            raise ValueError(f"Unknown event type: {event_type}")
        event_class = cls._event_classes[event_type]
        return event_class(event_type=event_type, payload=payload,
                           target_userid=target_userid, priority=priority, is_read=is_read, created_at=created_at, expires_at=expires_at,
                           **kwargs)

    def __init__(self, app:FunlabFlask):
        super().__init__(app)
        self.init_app(app)
        self.register_routes()

    def init_app(self, app: FunlabFlask):
        self.dbmgr = app.dbmgr
        self.sse_mgr = EventManager(self.dbmgr)
        self.app.teardown_appcontext(self.teardown)

    def teardown(self, exception):
        self.sse_mgr.shutdown()

    def sse_stream(self, user_id, event_types):
        def event_stream():
            user_stream = self.sse_mgr.register_user_stream(user_id)
            try:
                while True:
                    event = user_stream.get()
                    if event['type'] in event_types:
                        yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
            finally:
                self.sse_mgr.unregister_user_stream(user_id, user_stream)
        return Response(stream_with_context(event_stream()), content_type='text/event-stream')

    def register_routes(self):
        @self.blueprint.route('/events')
        @login_required
        def events_page():
            return render_template('events.html')

        @self.blueprint.route('/stream')
        @login_required
        def stream():
            return self.sse_stream(current_user.id)

        @self.blueprint.route('/api/events/mark_read/<int:event_id>', methods=['POST'])
        @login_required
        def mark_event_read(event_id):
            success = self.mark_event_as_read(event_id, current_user.id)
            return jsonify({'success': success})

    def get_user_events(self, user_id, event_type=None, priority=None, include_expired=False):
        stmt = select(EventEntity).where(or_(EventEntity.user_id == user_id, EventEntity.is_global == True))
        if not include_expired:
            stmt = stmt.where(or_(EventEntity.is_expired == False))
        if event_type:
            stmt = stmt.where(EventEntity.event_type == event_type)
        if priority:
            stmt = stmt.where(EventEntity.priority == priority.value)
        stmt = stmt.order_by(EventEntity.priority.desc(), EventEntity.created_at.desc())
        sa_session = self.app.dbmgr.get_db_session()
        result = sa_session.execute(stmt).scalars().all()
        return result

    def mark_event_as_read(self, event_id, user_id):
        sa_session = self.app.dbmgr.get_db_session()
        event: EventEntity = sa_session.execute(select(EventEntity).where(EventEntity.id == event_id)).scalar_one_or_none()
        if event and (event.user_id == user_id or event.is_global):
            event.is_read = True
            sa_session.commit()
            return True
        return False


    def start_service(self):
        pass

    def stop_service(self):
        pass

    def restart_service(self):
        self.stop_service()
        self.start_service()

    def reload_service(self):
        pass
