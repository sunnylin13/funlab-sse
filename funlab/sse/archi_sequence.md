``` mermaid
sequenceDiagram
    participant User
    participant FlaskApp
    participant EnhancedEventNotificationSystem
    participant EventStorage
    participant ConnectionManager
    participant Metrics

    User->>FlaskApp: Request /events/stream
    FlaskApp->>EnhancedEventNotificationSystem: Register user stream
    EnhancedEventNotificationSystem->>ConnectionManager: Add connection
    ConnectionManager-->>EnhancedEventNotificationSystem: Connection added
    EnhancedEventNotificationSystem-->>FlaskApp: Stream events

    User->>FlaskApp: Request /metrics
    FlaskApp->>EnhancedEventNotificationSystem: Get metrics
    EnhancedEventNotificationSystem->>Metrics: Retrieve metrics
    Metrics-->>EnhancedEventNotificationSystem: Metrics data
    EnhancedEventNotificationSystem-->>FlaskApp: Return metrics

    EnhancedEventNotificationSystem->>EventStorage: Store event
    EventStorage-->>EnhancedEventNotificationSystem: Event stored

    EnhancedEventNotificationSystem->>ConnectionManager: Distribute event
    ConnectionManager-->>EnhancedEventNotificationSystem: Event distributed
    EnhancedEventNotificationSystem->>Metrics: Record event delivery
    Metrics-->>EnhancedEventNotificationSystem: Delivery recorded
```