/**
 * FunLab Notification Manager - SSE Mode
 *
 * Handles SSE-based notification display for the FunLab application.
 * Requires: sse_client.js (must be loaded before this script)
 *
 * Notification lifecycle (SSE mode)
 * ─────────────────────────────────────────────────────────
 *   New :  SSE push → renderNotification(is_recovered=false) → Toast + Banner
 *   Reload: (no server-side history for SSE) → notifications are lost on page reload
 *           (SSE plugin may implement its own history endpoint in the future)
 *   Dismiss single : sseClient.markEventRead(id)
 *   Clear all      : sseClient.markEventsRead(ids)
 */
document.addEventListener('DOMContentLoaded', function () {
    // -----------------------------------------------------------------------
    // 1. Ensure toast container exists
    // -----------------------------------------------------------------------
    let toastContainer = document.getElementById('notification-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'notification-container';
        document.body.appendChild(toastContainer);
    }

    // -----------------------------------------------------------------------
    // 2. Grab banner notification elements
    // -----------------------------------------------------------------------
    const bannerNotificationArea = document.getElementById('SystemNotification');
    const notificationBadge      = document.getElementById('notification-badge');
    const notificationFooter     = document.getElementById('notification-footer');
    const notificationDropdownToggle = bannerNotificationArea
        ? bannerNotificationArea.closest('.dropdown-menu').previousElementSibling
        : null;

    let unreadCount          = 0;
    let isAddingNotification = false;

    // Prevent dropdown from opening while programmatically adding notifications
    if (notificationDropdownToggle) {
        notificationDropdownToggle.addEventListener('show.bs.dropdown', function (event) {
            if (isAddingNotification) event.preventDefault();
        });
    }

    // -----------------------------------------------------------------------
    // 3. Badge & footer helpers
    // -----------------------------------------------------------------------
    function updateNotificationBadge() {
        if (!notificationBadge) return;
        if (unreadCount > 0) {
            notificationBadge.textContent = unreadCount > 99 ? '99+' : unreadCount;
            notificationBadge.classList.remove('d-none');
        } else {
            notificationBadge.classList.add('d-none');
        }
    }

    function updateFooterVisibility() {
        if (!notificationFooter) return;
        notificationFooter.classList.toggle('d-none', unreadCount <= 0);
    }

    // -----------------------------------------------------------------------
    // 4. Unified render function
    //
    //    is_recovered (server-set):
    //      false → brand-new notification  → Toast + Banner
    //      true  → already delivered before (page reload recovery) → Banner only
    // -----------------------------------------------------------------------
    function renderNotification(data /*, eventType */) {
        const isRecovered  = data.is_recovered || false;
        const isPersistent = data.is_persistent !== false;
        const payload      = data.payload;
        const eventId      = data.id;

        if (!eventId) {
            console.warn('[Notification] Event has no ID, skipping.', data);
            return;
        }

        // Avoid adding the same notification twice (e.g., race between SSE + poll)
        if (bannerNotificationArea &&
                bannerNotificationArea.querySelector(`[data-event-id="${eventId}"]`)) {
            return;
        }

        unreadCount++;
        updateNotificationBadge();
        updateFooterVisibility();

        // A. Toast – only for fresh (non-recovered) notifications
        if (!isRecovered) {
            _showToast(data, payload, eventId);
        }

        // B. Banner drop-down item – always
        if (bannerNotificationArea) {
            _addBannerItem(data, payload, eventId, isPersistent);
        }
    }

    function _showToast(data, payload, eventId) {
        const toastNotif = document.createElement('div');
        toastNotif.className = 'toast-notification';
        toastNotif.dataset.eventId = eventId;

        if (data.priority === 'HIGH' || data.priority === 'CRITICAL') {
            toastNotif.classList.add('high-priority');
        }

        const toastContent = document.createElement('div');
        toastContent.className = 'toast-content';
        toastContent.innerHTML = `<h5>${payload.title}</h5><p>${payload.message}</p>`;

        const closeBtn = document.createElement('button');
        closeBtn.className = 'toast-close-btn';
        closeBtn.innerHTML = '&times;';
        closeBtn.setAttribute('aria-label', 'Close notification');
        closeBtn.onclick = function () {
            toastNotif.style.opacity = '0';
            toastNotif.style.transform = 'translateX(100%)';
            setTimeout(() => toastNotif.remove(), 400);
        };

        toastNotif.appendChild(toastContent);
        toastNotif.appendChild(closeBtn);
        toastContainer.prepend(toastNotif);

        // Auto-dismiss after 3 s
        setTimeout(() => {
            if (toastNotif.parentElement) closeBtn.onclick();
        }, 3000);
    }

    function _addBannerItem(data, payload, eventId, isPersistent) {
        const isHighPriority = data.priority === 'HIGH' || data.priority === 'CRITICAL';
        const dotClass = isHighPriority ? 'status-dot-animated bg-red' : 'bg-blue';

        const listItem = document.createElement('div');
        listItem.className = 'list-group-item';
        listItem.dataset.eventId      = eventId;
        listItem.dataset.isPersistent = isPersistent ? '1' : '0';
        listItem.innerHTML = `
            <div class="row align-items-center">
                <div class="col-auto">
                    <span class="status-dot ${dotClass} d-block"></span>
                </div>
                <div class="col text-truncate">
                    <a href="#" class="text-body d-block">${payload.title}</a>
                    <div class="d-block text-muted text-truncate mt-n1">${payload.message}</div>
                </div>
                <div class="col-auto">
                    <a href="#" class="list-group-item-actions"
                       onclick="FunlabNotifications.closeBannerItem(event, this, '${eventId}')">
                        <svg xmlns="http://www.w3.org/2000/svg" class="icon text-muted"
                             width="24" height="24" viewBox="0 0 24 24"
                             stroke-width="2" stroke="currentColor" fill="none"
                             stroke-linecap="round" stroke-linejoin="round">
                            <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                            <path d="M18 6l-12 12M6 6l12 12"/>
                        </svg>
                    </a>
                </div>
            </div>`;
        bannerNotificationArea.insertBefore(listItem, bannerNotificationArea.firstChild);
    }

    // -----------------------------------------------------------------------
    // 5. Public API (attached to window for inline onclick handlers)
    // -----------------------------------------------------------------------
    window.FunlabNotifications = {

        /** Close a single banner item and remove it from the server */
        closeBannerItem: function (event, element, eventId) {
            event.preventDefault();
            event.stopPropagation();

            const listItem     = element.closest('.list-group-item');
            const isPersistent = listItem ? listItem.dataset.isPersistent !== '0' : true;

            if (listItem) {
                listItem.remove();
                unreadCount = Math.max(0, unreadCount - 1);
                updateNotificationBadge();
                updateFooterVisibility();
            }

            if (!eventId) return;

            if (typeof window.sseClient !== 'undefined' && isPersistent) {
                // SSE mode: mark read via SSE plugin API
                window.sseClient.markEventRead(eventId)
                    .then(() => console.debug('[Notification] SSE marked as read:', eventId))
                    .catch(err => console.error('[Notification] SSE markEventRead failed:', err));
            }
        },

        /** Clear all banner notifications and remove them from the server */
        clearAll: function (event) {
            event.preventDefault();
            event.stopPropagation();

            if (!bannerNotificationArea) return;
            const listItems = bannerNotificationArea.querySelectorAll('.list-group-item');
            if (listItems.length === 0) return;

            const persistentIds = Array.from(listItems)
                .filter(item => item.dataset.isPersistent !== '0')
                .map(item => item.dataset.eventId);

            bannerNotificationArea.innerHTML = '';
            unreadCount = 0;
            updateNotificationBadge();
            updateFooterVisibility();

            if (typeof window.sseClient !== 'undefined' && persistentIds.length > 0) {
                // SSE mode
                window.sseClient.markEventsRead(persistentIds)
                    .then(() => console.debug('[Notification] SSE all marked as read.'))
                    .catch(err => console.error('[Notification] SSE markEventsRead failed:', err));
            }
        },

        /** Expose renderNotification for external use (e.g., custom plugins) */
        render: renderNotification,
    };

    // -----------------------------------------------------------------------
    // 6. SSE mode: subscribe for new events
    // -----------------------------------------------------------------------
    if (typeof window.sseClient !== 'undefined') {
        window.sseClient.subscribe('SystemNotification', renderNotification);
        console.debug('[Notification] SSE subscribed to SystemNotification.');
    } else {
        console.error('[Notification] SSE client not found! Make sure sse_client.js is loaded before this script.');
    }

    // -----------------------------------------------------------------------
    // 7. Initial UI sync
    // -----------------------------------------------------------------------
    updateNotificationBadge();
    updateFooterVisibility();
});
