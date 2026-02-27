# SSE Plugin Consumer Guide (Application Integration View)

This guide explains how to integrate `funlab-sse` from the **consumer/application developer** perspective, rather than internal plugin implementation details.

## 1. What you get
After integrating `funlab-sse`, your app can provide:
- Real-time SSE notifications
- Unread event recovery for offline users
- Compatibility with existing notification provider routes

## 2. Intended readers
- Backend developers who need to send notifications from app logic
- Frontend developers maintaining notification UI
- QA/DevOps validating real-time and recovery behavior

## 3. Pre-integration checklist
1. Plugin is installed (`poetry install` or equivalent)
2. Main app plugin manager auto-loads `SSEService`
3. Frontend creates SSE connection to `/sse/SystemNotification`
4. Frontend keeps poll fallback (`/notifications/poll`)

## 4. Backend usage

### 4.1 Send user notification
```python
app.send_user_notification(
    title="Task Completed",
    message="Your export is ready",
    target_userid=123,
    priority="NORMAL",
)
```

### 4.2 Send global notification
```python
app.send_global_notification(
    title="System Notice",
    message="Deployment at 23:00",
    priority="HIGH",
)
```

### 4.3 Avoid hard internal coupling
Prefer app/provider interfaces instead of directly depending on internal `EventManager` structures.

## 5. Frontend usage

### 5.1 Stream endpoint
- `GET /sse/SystemNotification`

### 5.2 Required handling
- Normal event payload rendering
- Heartbeat handling
- Reconnect with backoff (recommended)
- Poll fallback after reload/reconnect for state alignment

## 6. Notification lifecycle (consumer view)
1. Server creates and stores event
2. Online users receive SSE immediately
3. Offline users recover unread events on reconnect
4. Dismiss/clear updates read state

## 7. Common integration mistakes
- Implementing SSE only without poll fallback
- Binding UI too tightly to one payload structure
- Ignoring dismiss APIs and causing repeated notifications
- Treating plugin internals as public stable API

## 8. Minimum acceptance checks
1. Real-time delivery works after connection
2. Offline-generated notifications recover on reconnect
3. Dismissed item does not reappear
4. Clear-all empties unread list

## 9. Document scope
- This document: **how to consume the module**
- `sse-plugin-development-guide.en.md`: **how to develop and maintain the module internals**
