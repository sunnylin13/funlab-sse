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

# class EventPriority(Enum):
#     LOW = 1
#     NORMAL = 2
#     HIGH = 3

# class EventType(Enum):
#     NOTIFICATION = "notification"
#     ALERT = "alert"
#     MESSAGE = "message"
#     SYSTEM = "system"

@dataclass
class SSEvent:
    event_type: str
    # priority: EventPriority
    user_id: int
    payload: dict




@entities_registry.mapped
@dataclass
class EventEntity(_Readable):
    __tablename__ = 'event'
    __sa_dataclass_metadata_key__ = 'sa'

    id: int = field(init=False, metadata={'sa': Column(Integer, primary_key=True, autoincrement=True)})
    event_type: str = field(metadata={'sa': Column(String(50), nullable=False)})
    # priority: int = field(metadata={'sa': Column(SQLEnum(EventPriority))})
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

# models.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, Enum
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class QueuedEvent(Base):
    __tablename__ = 'queued_events'
    
    id = Column(Integer, primary_key=True)
    event_id = Column(String(50), nullable=False)
    event_type = Column(String(50), nullable=False)
    data = Column(JSON, nullable=False)
    user_id = Column(Integer, nullable=True)
    is_global = Column(Boolean, default=False)
    priority = Column(Integer, default=2)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    processed = Column(Boolean, default=False)
    processing_attempts = Column(Integer, default=0)
    
    def to_event_dict(self):
        """將數據庫記錄轉換為事件字典"""
        return {
            'id': self.event_id,
            'type': self.event_type,
            'data': self.data,
            'user_id': self.user_id,
            'is_global': self.is_global,
            'priority': self.priority,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None
        }