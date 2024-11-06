# models.py
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import time
from flask import Response, stream_with_context
from funlab.core import _Readable
from sqlalchemy import JSON, Boolean, Column, Float, ForeignKey, Integer, String, UniqueConstraint, or_, select
from sqlalchemy.orm import Session
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.ext.hybrid import hybrid_property
# all of application's entity, use same registry to declarate
from funlab.core.appbase import APP_ENTITIES_REGISTRY as entities_registry

from sqlalchemy.orm import registry
entities_registry = registry()
@dataclass
class ServerSideEvent(_Readable):
    event_type: str
    payload: dict
    user_id: int = None
    is_read: bool = False
    created_at: float = datetime.now(timezone.utc).timestamp()
    expires_at: float = None

    @property
    def is_global(self):
        return self.user_id is None
    
    @property
    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc).timestamp() > self.expires_at

@entities_registry.mapped
@dataclass
class ServerSideEventEntity(ServerSideEvent):
    __tablename__ = 'server_side_event'
    __sa_dataclass_metadata_key__ = 'sa'

    id: int = field(init=False, metadata={'sa': Column(Integer, primary_key=True, autoincrement=True)})
    event_type: str = field(metadata={'sa': Column(String(50), nullable=False)})
    payload:JSON = field(metadata={'sa': Column(JSON, nullable=False)})
    user_id: int = field(default=None, metadata={'sa': Column(Integer, ForeignKey('user.id'), nullable=True)})
    is_read : bool = field(default=False, metadata={'sa': Column(Boolean, default=False, nullable=False)})
    created_at: float = field(default=datetime.now(timezone.utc).timestamp(), metadata={'sa': Column(Float, nullable=False)})
    expires_at: float = field(default=None, metadata={'sa': Column(Float, nullable=True)})

    @hybrid_property
    def is_global(self):
        return self.user_id is None

    @hybrid_property
    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc).timestamp() > self.expires_at

    def to_dto(self):  # ServerSideEvent, Data transfer object
        return ServerSideEvent(
            event_type=self.event_type,
            payload=self.payload,
            user_id=self.user_id,
            is_read=self.is_read,
            created_at=self.created_at,
            expires_at=self.expires_at
        )

    def to_json(self):
        return self.to_dto().to_json()
