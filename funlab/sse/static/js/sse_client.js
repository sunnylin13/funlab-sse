/**
 * Unified SSE client for FunLab
 * Handles Server-Sent Events connections and rendering
 */
class SSEClient {
    constructor(options = {}) {
        this.options = {
            reconnectTimeout: 1000,      // SSE-14：退避基準（含上限）
            maxReconnectTimeout: 60000,
            heartbeatInterval: 30000,
            debug: false,
            ...options
        };
        this.eventSources = {};
        this.connected = false;
        this.renderFunctions = {};
        this._retryCounts = {};          // eventType -> 連續失敗次數
        this._reconnectTimers = {};      // eventType -> timer id
        this._unloadHandlers = {};       // eventType -> bound handler
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
            this._retryCounts[eventType] = 0;      // SSE-14：連上即歸零退避
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

        // SSE-14 重連策略：CLOSED（終端態，如 HTTP 4xx/5xx）才手動指數退避；
        // CONNECTING 態交給 EventSource 原生重試，避免雙重連線競態。
        eventSource.onerror = (error) => {
            console.warn(`SSE connection error for ${eventType}:`, error);
            if (eventSource.readyState === EventSource.CLOSED) {
                this.connected = false;
                const attempt = (this._retryCounts[eventType] || 0) + 1;
                this._retryCounts[eventType] = attempt;
                const delay = Math.min(
                    this.options.reconnectTimeout * Math.pow(2, attempt - 1),
                    this.options.maxReconnectTimeout
                ) * (0.5 + Math.random());        // jitter 0.5x–1.5x
                console.log(`Reconnecting ${eventType} in ${Math.round(delay)}ms (attempt ${attempt})`);
                clearTimeout(this._reconnectTimers[eventType]);
                this._reconnectTimers[eventType] = setTimeout(() => {
                    this.subscribe(eventType, renderFunction, endpoint);
                }, delay);
            }
        };

        // SSE-14：每個 eventType 只掛一次卸載監聽器，unsubscribe 時移除
        const unloadHandler = () => this.unsubscribe(eventType);
        this._unloadHandlers[eventType] = unloadHandler;
        window.addEventListener('beforeunload', unloadHandler);

        return eventSource;
    }

    /**
     * Unsubscribe from an event type
     * @param {string} eventType - Type of event to unsubscribe from
     */
    unsubscribe(eventType) {
        clearTimeout(this._reconnectTimers[eventType]);
        delete this._reconnectTimers[eventType];
        if (this._unloadHandlers[eventType]) {
            window.removeEventListener('beforeunload', this._unloadHandlers[eventType]);
            delete this._unloadHandlers[eventType];
        }
        if (this.eventSources[eventType]) {
            this.eventSources[eventType].close();
            delete this.eventSources[eventType];
            if (this.options.debug) {
                console.log(`Unsubscribed from ${eventType}`);
            }
        }
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

        return fetch('/notifications/dismiss', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ ids: [eventId] })
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

        return fetch('/notifications/dismiss', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ ids: eventIds })
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

// Global instance - exposed to window for use by other scripts
// SSE-14：正式環境預設靜音；需要除錯時控制台執行
//   window.sseClient.options.debug = true
window.sseClient = new SSEClient({ debug: false });
