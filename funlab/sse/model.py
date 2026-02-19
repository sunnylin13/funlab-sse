"""
Canonical SSE event models for funlab-sse plugin.

This module is the authoritative source for SSE-related SQLAlchemy entities.
funlab-flaskr/sse/models.py acts as a shim that imports from here when this
package is installed, avoiding duplicate APP_ENTITIES_REGISTRY registrations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import get_type_hints

from funlab.core import _Readable
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, and_, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.ext.hybrid import hybrid_property
from tzlocal import get_localzone

# All application entities share the same registry for cross-package table
# awareness (FK resolution, joined-table inheritance, etc.)
from funlab.core.appbase import APP_ENTITIES_REGISTRY as entities_registry


# ---------------------------------------------------------------------------
# Payload base
# ---------------------------------------------------------------------------

@dataclass
class PayloadBase:
    """Base class for strongly-typed event payloads."""

    @classmethod
    def from_jsonstr(cls, payload_str: str) -> 'PayloadBase':
        data = json.loads(payload_str)
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    def __str__(self) -> str:
        return self.to_json()


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

class EventPriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# ---------------------------------------------------------------------------
# EventBase  (pure-Python dataclass, no DB mapping)
# ---------------------------------------------------------------------------

@dataclass
class EventBase(_Readable):
    """In-memory representation of a single, user-targeted SSE event.

    Design notes
    ------------
    * ``target_userid`` is always set -- global broadcasts are handled by the
      caller iterating over online users and creating individual events.
    * ``is_read`` tracks per-user delivery acknowledgement.
    * ``is_recovered`` marks events that were loaded from DB on reconnect.
    """

    id: int = field(init=False)
    event_type: str = field(init=False)
    payload: PayloadBase = field(init=False)
    target_userid: int
    priority: EventPriority = EventPriority.NORMAL
    is_read: bool = field(init=False, default=False)
    is_recovered: bool = field(init=False, default=False)
    created_at: datetime = field(init=False, default_factory=lambda: datetime.now(timezone.utc))
    expired_at: datetime = None

    def __init__(
        self,
        target_userid: int = None,
        priority: EventPriority = EventPriority.NORMAL,
        expired_at: datetime = None,
        payload: PayloadBase = None,
        **payload_kwargs,
    ):
        self.id = None
        self.event_type = self.__class__.__name__.removesuffix('Event')
        if payload:
            self.payload = payload
        else:
            payload_cls = get_type_hints(self.__class__).get('payload', self.__annotations__['payload'])
            self.payload = payload_cls(**payload_kwargs)
        self.target_userid = target_userid
        self.priority = priority
        self.is_read = False
        self.is_recovered = False
        self.created_at = datetime.now(timezone.utc)
        self.expired_at = expired_at

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_expired(self) -> bool:
        return bool(self.expired_at and datetime.now(timezone.utc) > self.expired_at)

    @property
    def local_created_at(self) -> datetime:
        """created_at converted to the local timezone."""
        return self.created_at.astimezone(get_localzone())

    @property
    def local_expires_at(self):
        """expired_at converted to the local timezone (None if no expiry)."""
        return self.expired_at.astimezone(get_localzone()) if self.expired_at else None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        return super().to_json()

    def to_dict(self) -> dict:
        """Serialise to a dict suitable for JSON-encoding and sending to the browser."""
        return {
            "id": self.id,
            "event_type": self.event_type,
            "priority": self.priority.name,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload.__dict__ if self.payload else {},
            "is_recovered": self.is_recovered,
        }

    # ------------------------------------------------------------------
    # DB round-trip helpers
    # ------------------------------------------------------------------

    def to_entity(self):
        """Convert to an EventEntity for persistence.

        Returns None when the event is already read/expired (nothing to store).
        """
        if self.is_read or self.is_expired:
            return None
        entity = EventEntity(
            event_type=self.event_type,
            payload=self.payload.to_json(),
            target_userid=self.target_userid,
            priority=self.priority,
            expired_at=self.expired_at,
        )
        entity.is_read = self.is_read
        entity.created_at = self.created_at
        return entity

    @classmethod
    def from_entity(cls, entity: 'EventEntity'):
        """Reconstruct an in-memory event from a DB row.

        Returns None when the entity is already read/expired.
        """
        if entity.is_read or entity.is_expired:
            return None
        payload_cls = get_type_hints(cls).get('payload', cls.__annotations__['payload'])
        payload_obj = (
            payload_cls.from_jsonstr(entity.payload)
            if isinstance(entity.payload, str)
            else entity.payload
        )
        event = cls(
            target_userid=entity.target_userid,
            priority=entity.priority,
            expired_at=entity.expired_at,
            payload=payload_obj,
        )
        event.id = entity.id
        event.is_read = entity.is_read
        event.created_at = entity.created_at
        return event

    def sse_format(self) -> str:
        """Legacy SSE wire format (deprecated -- prefer to_dict())."""
        return f"event: {self.event_type}\ndata: {self.payload.to_json()}\n\n"


# ---------------------------------------------------------------------------
# EventEntity  (SQLAlchemy-mapped DB table)
# ---------------------------------------------------------------------------

@entities_registry.mapped
@dataclass
class EventEntity(EventBase):
    """Persistent representation of an SSE event stored in the ``event`` table."""

    __tablename__ = 'event'
    __sa_dataclass_metadata_key__ = 'sa'

    id: int = field(
        init=False,
        metadata={'sa': Column(Integer, primary_key=True, autoincrement=True)},
    )
    event_type: str = field(
        metadata={'sa': Column(String(50), nullable=False)},
    )
    payload: PayloadBase = field(
        metadata={'sa': Column(JSON, nullable=False)},
    )
    target_userid: int = field(
        default=None,
        # Plain Integer — no ORM-level FK to 'user.id'.
        # SSE is a cross-plugin concern: it stores a user-id as an opaque
        # integer filter value only. Declaring ForeignKey('user.id') here
        # would force funlab-auth to be imported before the SSE table can
        # be created, breaking plugin isolation.  DB-level FK integrity is
        # the responsibility of migration tooling (Alembic), not the mapper.
        metadata={'sa': Column(Integer, nullable=True)},
    )
    priority: EventPriority = field(
        default=None,
        metadata={'sa': Column(SQLEnum(EventPriority), default=EventPriority.NORMAL, nullable=False)},
    )
    is_read: bool = field(
        init=False,
        default=False,
        metadata={'sa': Column(Boolean, nullable=False, default=False)},
    )
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
        metadata={'sa': Column(DateTime(timezone=True), nullable=False)},
    )
    expired_at: datetime = field(
        default=None,
        metadata={'sa': Column(DateTime(timezone=True), nullable=True)},
    )

    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expired_at and datetime.now(timezone.utc) > self.expired_at)

    @is_expired.expression
    def is_expired(cls):
        return and_(cls.expired_at.isnot(None), cls.expired_at < func.now())


# ---------------------------------------------------------------------------
# Built-in event types
# ---------------------------------------------------------------------------

@dataclass
class SystemNotificationPayload(PayloadBase):
    """Payload for user-visible system notification toasts."""
    title: str
    message: str


@dataclass(init=False)
class SystemNotificationEvent(EventBase):
    """Standard in-app notification event (SSE event type: SystemNotification)."""
    payload: SystemNotificationPayload
