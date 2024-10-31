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

class EventPriority(Enum):
    LOW = 1
    NORMAL = 2
    HIGH = 3

class EventType(Enum):
    NOTIFICATION = "notification"
    ALERT = "alert"
    MESSAGE = "message"
    SYSTEM = "system"

# @dataclass
# class SSEvent:
#     event_type: str
#     priority: EventPriority
#     payload: dict

#     def to_json(self):
#         return {
#             'event_type': self.event_type,
#             'priority': self.priority,
#             'data': self.data
#         }

@entities_registry.mapped
@dataclass
class EventEntity(_Readable):
    __tablename__ = 'event'
    __sa_dataclass_metadata_key__ = 'sa'

    id: int = field(init=False, metadata={'sa': Column(Integer, primary_key=True, autoincrement=True)})
    event_type: str = field(metadata={'sa': Column(String(50), nullable=False)})
    priority:EventPriority = field(metadata={'sa': Column(SQLEnum(EventPriority))})
    payload:JSON = field(metadata={'sa': Column(JSON, nullable=False)})
    user_id: int = field(metadata={'sa': Column(Integer, ForeignKey('user.id'), nullable=True)})
    is_read : bool = field(init=False, metadata={'sa': Column(Boolean, default=False, nullable=True)})
    created_at: float = field(init=False, default=datetime.now(timezone.utc), metadata={'sa': Column(Float, nullable=True)})
    expires_at: float = field(default=datetime.now(timezone.utc), metadata={'sa': Column(Float, nullable=True)})

    @hybrid_property
    def is_global(self):
        return self.user_id is None

    @hybrid_property
    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc) > self.expires_at

