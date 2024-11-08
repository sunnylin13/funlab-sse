# models.py
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import time
from flask import Response, stream_with_context
from funlab.core import _Readable
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, or_, select
from sqlalchemy.orm import Session
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.ext.hybrid import hybrid_property
# all of application's entity, use same registry to declarate
from funlab.core.appbase import APP_ENTITIES_REGISTRY as entities_registry
#from sqlalchemy.orm import registry
#entities_registry = registry()
from tzlocal import get_localzone
class PayloadBase(BaseModel):   
    @classmethod
    def from_jsonstr(cls, payload_str: str) -> 'PayloadBase':
        return cls.model_validate_json(payload_str)
    
    def to_json(self):
        return self.model_dump_json()
    
class EventPriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3
@dataclass
class BaseEvent(_Readable):
    event_type: str
    payload: PayloadBase
    target_userid: int = None
    priority: EventPriority = EventPriority.NORMAL
    is_read: bool = False
    created_at: datetime = datetime.now(timezone.utc)
    expires_at: datetime = None

    @property
    def is_global(self):
        return self.target_userid is None
    
    @property
    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc) > self.expires_at
    
    def to_json(self):
        return super().to_json()
    
    def to_sse(self):
        """ Format the event as a Server-Sent Event. """
        return f"event: {self.event_type}\ndata: {self.payload.to_json()}\n\n"


@entities_registry.mapped
@dataclass
class EventEntity(BaseEvent):
    __tablename__ = 'event'
    __sa_dataclass_metadata_key__ = 'sa'

    id: int = field(init=False, metadata={'sa': Column(Integer, primary_key=True, autoincrement=True)})
    event_type: str = field(metadata={'sa': Column(String(50), nullable=False)})
    payload: PayloadBase = field(metadata={'sa': Column(JSON, nullable=False)})
    target_userid: int = field(default=None, metadata={'sa': Column(Integer, ForeignKey('user.id'), nullable=True)})
    priority: EventPriority = field(default=None, metadata={'sa': Column(SQLEnum(EventPriority), default=EventPriority.NORMAL, nullable=False)})
    is_read: bool = field(default=False, metadata={'sa': Column(Boolean, default=False, nullable=False)})
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc), metadata={'sa': Column(DateTime(timezone=True), nullable=False)})
    expires_at: datetime = field(default=None, metadata={'sa': Column(DateTime(timezone=True), nullable=True)})

    def post_init(self):
        self.payload = PayloadBase.from_jsonstr(self.payload)  # Convert payload from JSON string to object

    @hybrid_property
    def is_global(self):
        return self.target_userid is None

    @hybrid_property
    def is_expired(self):
        return self.expires_at and datetime.now(timezone.utc) > self.expires_at

    def to_dto(self):  # BaseEvent, Data transfer object
        if isinstance(self.payload, str):
            payload = PayloadBase.from_jsonstr(self.payload)
        else:
            payload = self.payload

        return BaseEvent(
            event_type=self.event_type,
            payload=payload,
            target_userid=self.target_userid,
            priority=self.priority,
            is_read=self.is_read,
            created_at=self.created_at,
            expires_at=self.expires_at
        )

    def to_json(self):
        return self.to_dto().to_json()
    
    @property
    def local_created_at(self):
        """Convert created_at to the local timezone for display."""
        local_tz = get_localzone()
        return self.created_at.astimezone(local_tz)

    @property
    def local_expires_at(self):
        """Convert expires_at to the local timezone for display."""
        if self.expires_at:
            local_tz = get_localzone()
            return self.expires_at.astimezone(local_tz)
        return None
    
class EventFactory:
    _event_classes: dict[str, type[BaseEvent]] = {}

    @classmethod
    def register_event(cls, event_type: str, event_class: type[BaseEvent]):
        cls._event_classes[event_type] = event_class

    @classmethod
    def create_event(cls, event_type: str, payload: PayloadBase, 
                    target_userid: int = None, priority: EventPriority = EventPriority.NORMAL, is_read: bool = False, 
                    created_at: datetime = datetime.now(timezone.utc), expires_at: datetime = None, **kwargs) -> BaseEvent:
        if event_type not in cls._event_classes:
            raise ValueError(f"Unknown event type: {event_type}")
        event_class = cls._event_classes[event_type]
        return event_class(event_type=event_type, payload=payload, 
                           target_userid=target_userid, priority=priority, is_read=is_read, created_at=created_at, expires_at=expires_at,
                           **kwargs)
    
class TaskCompletedPayload(PayloadBase):
    task_name: str
    task_result: str
    task_start_time: datetime
    task_end_time: datetime

class TaskCompletedEvent(BaseEvent):
    event_type = 'task_completed'
    payload: TaskCompletedPayload

    def __init__(self, task_name: str, task_result: str, task_start_time: datetime, task_end_time: datetime, 
                 target_userid: int = None, priority: EventPriority = EventPriority.NORMAL, is_read: bool = False, 
                 created_at: datetime = datetime.now(timezone.utc), expires_at: datetime = None):
        self.payload = TaskCompletedPayload(task_name=task_name, task_result=task_result, task_start_time=task_start_time, task_end_time=task_end_time)
        self.target_userid = target_userid
        self.priority = priority
        self.is_read = is_read
        self.created_at = created_at
        self.expires_at = expires_at

    def __str__(self):
        return f"TaskCompletedEvent(task_name={self.payload.task_name}, task_result={self.payload.task_result}, task_start_time={self.payload.task_start_time}, task_end_time={self.payload.task_end_time}, target_userid={self.target_userid}, priority={self.priority}, is_read={self.is_read}, created_at={self.created_at}, expires_at={self.expires_at})"

    def __repr__(self):
        return self.__str__()

