"""funlab-sse: SSE plugin for the funlab framework.

Re-exports the canonical SSE models and the EventManager so that
funlab-flaskr can import from this package when it is installed.
"""
from .model import (
    PayloadBase,
    EventPriority,
    EventBase,
    EventEntity,
    SystemNotificationPayload,
    SystemNotificationEvent,
)
from .manager import EventManager, ConnectionManager
from .service import SSEService

__all__ = [
    "PayloadBase",
    "EventPriority",
    "EventBase",
    "EventEntity",
    "SystemNotificationPayload",
    "SystemNotificationEvent",
    "EventManager",
    "ConnectionManager",
    "SSEService",
]
