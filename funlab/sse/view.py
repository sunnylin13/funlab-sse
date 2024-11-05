from datetime import datetime, timedelta, timezone
import json
import time

from flask import (Response, flash, g, jsonify, redirect, render_template, request, stream_with_context,
                   url_for, current_app)
from flask_login import current_user, login_required
from funlab.core.menu import Menu, MenuItem
from funlab.core.plugin import ViewPlugin
from sqlalchemy import and_, or_, select, update

from flask_restx import Api, Resource, Namespace

from funlab.flaskr.app import FunlabFlask
from .model import ServerSideEvent, ServerSideEventEntity
from .manager import ServerSideEventMgr

class SSEView(ViewPlugin):

    def __init__(self, app:FunlabFlask):
        super().__init__(app)
        self.register_routes()

    def init_app(self, app: FunlabFlask):
        super().__init__(app)
        self.dbmgr = app.dbmgr
        self.app.teardown_appcontext(self.teardown)

    def teardown(self, exception):
        sse_mgr:ServerSideEventMgr = g.pop('sse_mgr', None)
        if sse_mgr is not None:
            sse_mgr.shutdown()

    @property
    def sse_mgr(self):
        if 'sse_mgr' not in g:
            g.sse_mgr = ServerSideEventMgr(self.dbmgr)
        return g.sse_mgr

    def sse_stream(self, user_id):
        def event_stream():
            user_stream = self.sse_mgr.register_user_stream(user_id)
            try:
                while True:
                    event = user_stream.get()
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

    def create_event(self, event_type:str, data:dict, user_id=None, is_global=True, expires_in_hours=24):
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
        event = ServerSideEventEntity(
            event_type=event_type,
            data=data,
            user_id=user_id,
            is_global=is_global,
            expires_at=expires_at
        )
        sa_session = self.app.dbmgr.get_db_session()
        sa_session.add(event)
        sa_session.commit()
        return event

    def get_user_events(self, user_id, event_type=None, priority=None, include_expired=False):
        stmt = select(ServerSideEventEntity).where(or_(ServerSideEventEntity.user_id == user_id, ServerSideEventEntity.is_global == True))
        if not include_expired:
            stmt = stmt.where(or_(ServerSideEventEntity.is_expired == False))
        if event_type:
            stmt = stmt.where(ServerSideEventEntity.event_type == event_type)
        if priority:
            stmt = stmt.where(ServerSideEventEntity.priority == priority.value)
        stmt = stmt.order_by(ServerSideEventEntity.priority.desc(), ServerSideEventEntity.created_at.desc())
        sa_session = self.app.dbmgr.get_db_session()
        result = sa_session.execute(stmt).scalars().all()
        return result

    def mark_event_as_read(self, event_id, user_id):
        sa_session = self.app.dbmgr.get_db_session()
        event: ServerSideEventEntity = sa_session.execute(select(ServerSideEventEntity).where(ServerSideEventEntity.id == event_id)).scalar_one_or_none()
        if event and (event.user_id == user_id or event.is_global):
            event.is_read = True
            sa_session.commit()
            return True
        return False