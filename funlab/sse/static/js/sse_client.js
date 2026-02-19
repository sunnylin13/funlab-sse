/**
 * Unified SSE client for FunLab
 * Handles Server-Sent Events connections and rendering
 */
class SSEClient {
    constructor(options = {}) {
        this.options = {
            reconnectTimeout: 5000,
            heartbeatInterval: 30000,
            debug: false,
            ...options
        };
        this.eventSources = {};
        this.connected = false;
        this.renderFunctions = {};
    }

    /**
     * Subscribe to an event type
     * @param {string} eventType - Type of event to subscribe to
     * @param {Function} renderFunction - Function to call when event is received
     * @param {string} endpoint - Optional custom endpoint
     */
    subscribe(eventType, renderFunction, endpoint = null) {
        if (this.options.debug) {
            console.log(`Subscribing to event: ${eventType}`);
        }

        // Close existing connection if any
        this.unsubscribe(eventType);

        // Create new EventSource
        const eventSource = new EventSource(endpoint || `/sse/${eventType}`);
        this.eventSources[eventType] = eventSource;
        this.renderFunctions[eventType] = renderFunction;

        // Set up event handlers
        eventSource.onopen = () => {
            this.connected = true;
            if (this.options.debug) {
                console.log(`Connection to ${eventType} opened`);
            }
        };

        // Listen for specific event type
        eventSource.addEventListener(eventType, (event) => {
            if (this.options.debug) {
                console.log(`Event received for ${eventType}:`, event);
            }

            try {
                const data = JSON.parse(event.data);
                // The server now sends the full event object.
                // Pass the whole object to the render function.
                renderFunction(data, data.event_type);
            } catch (error) {
                console.error("Failed to parse event data:", error);
            }
        });

        // Handle heartbeats
        eventSource.addEventListener('heartbeat', (event) => {
            if (this.options.debug) {
                console.log("Heartbeat received");
            }
        });

        // Error handling with reconnection
        eventSource.onerror = (error) => {
            console.error(`SSE connection error for ${eventType}:`, error);

            if (eventSource.readyState === EventSource.CLOSED) {
                this.connected = false;
                console.log(`Connection closed for ${eventType}, attempting to reconnect...`);

                setTimeout(() => {
                    this.subscribe(eventType, renderFunction, endpoint);
                }, this.options.reconnectTimeout);
            }
        };

        // Clean up on page unload
        window.addEventListener('beforeunload', () => {
            this.unsubscribe(eventType);
        });

        return eventSource;
    }

    /**
     * Unsubscribe from an event type
     * @param {string} eventType - Type of event to unsubscribe from
     */
    unsubscribe(eventType) {
        if (this.eventSources[eventType]) {
            this.eventSources[eventType].close();
            delete this.eventSources[eventType];
            if (this.options.debug) {
                console.log(`Unsubscribed from ${eventType}`);
            }
        }
    }

    /**
     * Send a notification via POST request
     * @param {string} title - Notification title
     * @param {string} message - Notification message
     * @param {string} endpoint - API endpoint
     * @returns {Promise} - Promise resolving to response data
     */
    sendNotification(title, message, endpoint = '/generate_notification') {
        const formData = new FormData();
        formData.append('title', title);
        formData.append('message', message);

        return fetch(endpoint, {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .catch(error => {
            console.error('Error sending notification:', error);
            throw error;
        });
    }

    /**
     * Mark an event as read on the server
     * @param {number} eventId - ID of the event to mark as read
     * @returns {Promise} - Promise resolving to response data
     */
    markEventRead(eventId) {
        if (this.options.debug) {
            console.log(`正在標記事件 ${eventId} 為已讀...`);
        }

        if (!eventId) {
            console.error('markEventRead: eventId 是空的或無效的');
            return Promise.reject(new Error('Invalid eventId'));
        }

        return fetch(`/mark_event_read/${eventId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            }
        })
        .then(response => {
            if (this.options.debug) {
                console.log(`標記事件 ${eventId} 響應狀態:`, response.status);
            }

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            return response.json();
        })
        .then(data => {
            if (this.options.debug) {
                console.log(`事件 ${eventId} 已成功標記為已讀:`, data);
            }
            return data;
        })
        .catch(error => {
            console.error(`標記事件 ${eventId} 為已讀時發生錯誤:`, error);
            throw error;
        });
    }

    /**
     * Mark multiple events as read on the server
     * @param {Array<number>} eventIds - Array of event IDs to mark as read
     * @returns {Promise} - Promise resolving to response data
     */
    markEventsRead(eventIds) {
        if (this.options.debug) {
            console.log(`正在標記多個事件為已讀...`, eventIds);
        }

        if (!eventIds || eventIds.length === 0) {
            console.error('markEventsRead: eventIds is empty or invalid');
            return Promise.reject(new Error('Invalid eventIds'));
        }

        return fetch(`/mark_events_read`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ event_ids: eventIds })
        })
        .then(response => {
            if (this.options.debug) {
                console.log(`標記多個事件響應狀態:`, response.status);
            }

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            return response.json();
        })
        .then(data => {
            if (this.options.debug) {
                console.log(`多個事件已成功標記為已讀:`, data);
            }
            return data;
        })
        .catch(error => {
            console.error(`標記多個事件為已讀時發生錯誤:`, error);
            throw error;
        });
    }
}

// Global instance
const sseClient = new SSEClient({ debug: true });
