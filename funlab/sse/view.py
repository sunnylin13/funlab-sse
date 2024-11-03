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

    # @property
    # def entities_registry(self):
    #     """ FunlabFlask use to table creation by sqlalchemy in __init__ for application initiation """
    #     return entities_registry

    def init_app(self, app: FunlabFlask):
        super().__init__(app)
        self.dbmgr = app.dbmgr
        self.app.extensions['sse'] = self
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
            while True:
                time.sleep(1)
                events: list[EventEntity] = self.get_user_events(user_id)
                for event in events:
                    yield f"event: {event.event_type}\ndata: {event.content}\n\n"

                # should delete the event after it is read
                #     sa_session.delete(event)
                # sa_session.commit()
        return Response(stream_with_context(event_stream()), content_type='text/event-stream')

    def register_routes(self):

        @self.blueprint.route('/events')
        @login_required
        def events_page():
            return render_template('events.html')

        @self.blueprint.route('/stream')
        @login_required
        def stream():
            def event_stream():
                last_check = datetime.now(timezone.utc)
                while True:
                    events = self.get_user_events(current_user.id)
                    new_events = [e for e in events if e.created_at > last_check]
                    for event in new_events:
                        # ssevent = SSEvent(event_type=event.event_type, priority=EventPriority(event.priority), data=event.payload)
                        yield f"event: {event.event_type}\ndata: {json.dumps(event.payload)}\n\n"
                    last_check = datetime.now(timezone.utc)
                    time.sleep(1)  # Check for new events every second

            return Response(stream_with_context(event_stream()), content_type='text/event-stream')

        @self.blueprint.route('/api/events/mark_read/<int:event_id>', methods=['POST'])
        @login_required
        def mark_event_read(event_id):
            success = self.mark_event_as_read(event_id, current_user.id)
            return jsonify({'success': success})

    def create_event(self, event_type:str, data:dict,
                     # priority:EventPriority=None,
                     user_id=None,
                    is_global=True, expires_in_hours=24):
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

        event = EventEntity(
            event_type=event_type,
            # priority=priority.value,
            data=data,
            user_id=user_id,
            is_global=is_global,
            expires_at=expires_at
        )
        sa_session = self.app.dbmgr.get_db_session()
        sa_session.add(event)
        sa_session.commit()
        return event

    def get_user_events(self, user_id,
                        event_type:EventType=None, priority: EventPriority=None, include_expired: bool=False):
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
        event: EventEntity = sa_session.execute(select(EventEntity).where(EventEntity.id == event_id)) # EventEntity.query.get(event_id)
        if event and (event.user_id == user_id or event.is_global):
            event.is_read = True
            sa_session.commit()
            return True
        return False