# funlab-sse 消費端指南（CONSUMER GUIDE）

適用對象：需要「發通知／推即時事件」的後端開發者、維護通知 UI 的前端工程師、驗收 SSE 行為的 QA。
內部實作與擴充請看 [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)。

## 1. 整合前檢查清單

1. 安裝：monorepo（uv workspace）內 `funlab-libs` 與 `funlab-sse` 一起同步安裝（`uv sync`）。
2. 主程式啟動時 plugin manager 依 entry point 自動載入 `SSEService`，並經 `app.set_notification_provider(self)`（`funlab/sse/service.py:SSEService._setup`）把 SSE 設為通知 provider。
3. 前端：登入後的頁面（繼承 `layouts/base.html`）由 `templates/includes/notification_init.html` 自動載入 `sse_client.js` ＋ `sse_notifications.js`（`sse_enabled` 時）。
4. 部署為單進程 WSGI（現況 waitress）。

## 2. 後端用法

### 2.1 发送持久化系統通知（推荐，經 app 層）

```python
app.send_user_notification(
    title='Task Completed',
    message='Your export is ready',
    target_userid=123,
    priority='NORMAL',      # 'LOW'|'NORMAL'|'HIGH'|'CRITICAL'
    expire_after=None,      # 過期秒數（介面契約；現行實作有單位缺陷，見 IMPROVEMENT_PLAN SSE-08：修復前請勿依賴此參數）
)

app.send_global_notification(
    title='System Notice',
    message='Deployment at 23:00',
    priority='HIGH',
)
```

事件寫入 DB（表 `event`），線上使用者即時送達；離線使用者於下次連線或 `/notifications/poll` 回補。

### 2.2 推送即時、非持久事件（例如報價 tick）

**唯一正確取法**——provider 掛在 `app.notification_provider`：

```python
prov = getattr(app, 'notification_provider', None)
if prov and prov.supports_realtime:
    prov.send_event(
        event_type='PriceUpdate',
        target_userid=user_id,
        payload={'symbol': '2330', 'price': '100.5'},
    )

# 查誰在線訂閱某事件型別：
users = app.notification_provider.get_connected_users('PriceUpdate')  # -> set[int]
```

介面契約：`funlab-libs/funlab/core/notification.py:INotificationProvider`
（`send_event(event_type, target_userid, payload, priority='NORMAL', expire_after=None) -> bool`、`get_connected_users(event_type) -> set`、`supports_realtime`）。
`send_event` 回 `True` 只代表「已入佇列」，使用者離線回 `False`；事件**不入 DB、不回補**（tick 類遺失屬預期，前端以 poll 端點對齊狀態）。

> ⚠️ **禁止 `app.sse_service`**：FunlabFlask 沒有這個屬性，`getattr(app, 'sse_service', None)` 恆為 `None`，推送會靜默失效（實案：finfun-quotesvcs，IMPROVEMENT_PLAN SSE-02/L14）。

> ⚠️ **已知現況缺陷（修復前即時事件送不达，勿誤判為整合錯誤）**：`send_event` 目前死在分發器（IMPROVEMENT_PLAN SSE-03）。持久通知（2.1）不受影響。

### 2.3 交易安全契約（重要）

**禁止在 `dbmgr.session_context()` 交易進行中呼叫 `send_user_notification` / `send_event` / `dismiss_*`**。
SSE 內部自帶 `session_context`，巢狀使用會把外層未提交交易提前 commit 並關閉（funlab-libs L1／IMPROVEMENT_PLAN SSE-06）。通知一律放在外層交易 commit **之後**。

## 3. 前端用法

### 3.1 訂閱串流

```javascript
// 全域實例由 sse_client.js 建立
window.sseClient.subscribe('PriceUpdate', (data, eventType) => {
    // data = { id, event_type, priority, created_at, payload, is_recovered }
    // 注意：send_event 推的即時事件 id 為 null
    render(data);
});
```

- 端點：`GET /sse/<event_type>`（同站台、需登入 cookie；`EventSource` 自帶 cookie）。
- 每個 event_type 一條連線；伺服器每 10 秒無事件時發 `heartbeat` 事件（sse_client.js 已內建監聽）。
- 斷線自動重連（sse_client.js 內建；退避品質見 IMPROVEMENT_PLAN SSE-14）。
- **不要自寫 `subscribeToEvent` 之類呼叫**——該函數不存在於現行程式（IMPROVEMENT_PLAN SSE-07）。

### 3.2 已讀／清空（POST，需 CSRF）

`/notifications/dismiss`（body `{"ids":[…]}`）與 `/notifications/clear` 受全域 CSRFProtect 保護。頁面必須含 `<meta name="csrf-token">` 且先載入 `static/js/csrf_ajax.js`（base.html 已內含，其 monkey-patch 會為同站非 GET fetch 自動附加 `X-CSRFToken`）。缺 meta 時 dismiss 一律 400。
前端慣用封裝：`sseClient.markEventRead(id)` / `sseClient.markEventsRead(ids)`。

### 3.3 生命周期

1. 新事件：SSE push → Toast ＋ Banner（`is_recovered=false`）。
2. 重新載入頁面：由 `/notifications/poll` 回補（`is_recovered=true`，只進 Banner 不彈 Toast）。
3. dismiss：標記已讀；已讀事件下次不再回補，並由週期性清理刪除。
4. 即時事件（id=null）：不入列表、不標已讀。

## 4. 權限與隱私（現況為正確設計，勿繞道）

- `/sse/*` 全部經過 plugin 政策中介層 `default_route_policy = is_authenticated_user`，未登入一律 403。
- 串流以 `current_user.id` 綁定，只會收到 `target_userid == 自己` 的事件；**沒有任何方法訂閱他人的事件**。
- 匿名 `/health` 等 flaskr 端點與 SSE 無關，不受本插件影響。

## 5. 常見整合錯誤

| 錯誤 | 後果 | 正確做法 |
|---|---|---|
| 用 `app.sse_service` | 靜默不送 | `app.notification_provider` |
| 在 session_context 中發通知 | 外層交易被提前提交 | commit 後再發 |
| 只做 SSE 不做 poll 對齊 | 重連前漏顯示 | reload 後呼叫 `/notifications/poll` |
| dismiss 不带 CSRF header | 400 | 用 base.html＋csrf_ajax.js，別繞開 |
| 依賴 `expire_after` 精確過期 | 單位缺陷（SSE-08） | 修復前自行控制生命週期 |

## 6. 驗收清單（最小可行）

1. 登入開頁 → 後端 `app.send_user_notification(...)` → 瀏覽器即時彈 Toast。
2. 關閉頁面 → 送通知 → 重開頁面 → Toast 不重複但 Banner 有回補項、badge 正確。
3. dismiss 單筆 → 不再出現（poll 不回補）。
4. clear all → 列表清空、poll 回傳空陣列。
5. 未登入直連 `/sse/SystemNotification` → 403。
6. （SSE-02/03 修復後）報價頁即時價格更新。
