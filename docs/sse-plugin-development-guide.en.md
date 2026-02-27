# SSE Plugin Usage and Development Guide (funlab-sse)

## 1. Purpose and Scope
This is the consolidated guide for `funlab-sse`, covering:
- Runtime usage (install, enable, basic operations)
- Module architecture and execution flow
- Development and extension practices
- Testing and troubleshooting guidance

> Current production path: `service.py` → `manager.py` → `model.py`.

---

## 2. Quick Start

### 2.1 Install
In the monorepo, use editable/development install:

```bash
cd funlab-sse
poetry install
```

### 2.2 Plugin Discovery
`pyproject.toml` already declares the plugin entry point:
- group: `funlab_plugin`
- plugin: `SSEService = "funlab.sse.service:SSEService"`

At app startup, `ModernPluginManager` discovers and loads it automatically.

### 2.3 Routes and Interfaces
- Stream endpoint: `GET /sse/<event_type>`
- Notification aggregation routes (provided by root blueprint):
  - `GET /notifications/poll`
  - `POST /notifications/clear`
  - `POST /notifications/dismiss`

---

## 3. Module Architecture

### 3.1 `SSEService` (`funlab/sse/service.py`)
Responsibilities:
- Register `SystemNotificationEvent`
- Initialize `EventManager`
- Set itself as `notification_provider`
- Implement `INotificationProvider` methods (send/fetch/dismiss)
- Expose SSE stream route and heartbeat behavior

Lifecycle highlights:
- Startup: `_setup()` creates table + initializes manager
- Shutdown: `_on_stop()` calls `EventManager.shutdown()`

### 3.2 `EventManager` (`funlab/sse/manager.py`)
Responsibilities:
- Create and distribute events via in-memory queue
- Recover unread events from DB on reconnect
- Periodically clean read/expired events
- Manage distributor/cleanup background threads

### 3.3 `EventEntity / EventBase` (`funlab/sse/model.py`)
Responsibilities:
- `EventEntity`: persistent database model (table `event`)
- `EventBase`: in-memory event object + serialization
- `SystemNotificationEvent`: built-in event type

Design rules:
- `target_userid` stays as an integer field; avoid ORM-level hard FK to other plugin tables
- `is_expired` provides a hybrid expression for safe SQL filtering

---

## 4. Usage Examples

### 4.1 Send a user notification
Called from app-side helpers (delegated to provider):

```python
app.send_user_notification(
    title="Build Complete",
    message="Your report is ready",
    target_userid=123,
    priority="NORMAL",
)
```

### 4.2 Send a global notification

```python
app.send_global_notification(
    title="System Notice",
    message="Maintenance tonight 23:00",
    priority="HIGH",
)
```

### 4.3 Client-side stream
Clients should connect to:
- `/sse/SystemNotification`

And handle:
- `event: <event_type>` payload messages
- heartbeat events (to avoid idle disconnect ambiguity)

---

## 5. Development Best Practices

1. **Change only the active path**: implement new features in `service.py` / `manager.py` / `model.py`.
2. **Avoid cross-plugin hard coupling**: do not add ORM mapper-level FK dependencies to external plugin tables.
3. **Use migration for schema changes**: evolve DB schema through Alembic/deployment, not runtime imports.
4. **Keep provider contract stable**: preserve `INotificationProvider` behavior for root routes compatibility.
5. **Prioritize thread safety**: review lock scopes and shutdown behavior when changing queues/connections.
6. **Improve observability**: log at least user_id, event_type, and stream_id (when available) for failures.

---

## 6. Testing Guidance

### 6.1 Manual checks
1. Sign in and open the notification UI.
2. Trigger `send_user_notification`.
3. Verify real-time delivery via SSE.
4. Close the page, create notifications, reconnect, verify recovery via poll/stream.
5. Verify `dismiss` / `clear` updates state correctly.

### 6.2 Automation ideas
- Unit tests:
  - `EventBase` ↔ `EventEntity` round-trip
  - `is_expired` query behavior
- Integration tests:
  - stream registration + heartbeat
  - reconnect recovery
  - dismiss APIs affecting DB state

---

## 7. Documentation Governance

- This file is the primary development guide for SSE plugin work.
- Legacy prototypes are moved under `funlab/sse/legacy/` and should not be imported by new code.
- When architecture changes, update this guide and README links together.
