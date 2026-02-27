# funlab-sse

Server-Sent Events (SSE) plugin for the Funlab Flask ecosystem.

## Language / 語言

- English guide: [docs/sse-plugin-development-guide.en.md](docs/sse-plugin-development-guide.en.md)
- Consumer guide (English): [docs/sse-plugin-consumer-guide.en.md](docs/sse-plugin-consumer-guide.en.md)
- 正體中文指引：[docs/sse-plugin-development-guide.zh-TW.md](docs/sse-plugin-development-guide.zh-TW.md)
- 使用者視角指引（正體中文）：[docs/sse-plugin-consumer-guide.zh-TW.md](docs/sse-plugin-consumer-guide.zh-TW.md)

---

## English

### What this plugin provides
- Real-time event streaming via `GET /sse/<event_type>`.
- Notification-provider integration with root routes:
	- `GET /notifications/poll`
	- `POST /notifications/clear`
	- `POST /notifications/dismiss`
- Persistent event recovery through database-backed `EventEntity`.

### Project structure (core)
- `funlab/sse/service.py`: Plugin service and provider integration.
- `funlab/sse/manager.py`: Event queue, distribution, recovery, cleanup.
- `funlab/sse/model.py`: Event models and persistence entity.

### Development best practices
1. Implement new features on the active path (`service.py` / `manager.py` / `model.py`).
2. Avoid hard ORM coupling to other plugin tables.
3. Apply schema changes through migration workflows (not runtime cross-plugin imports).
4. Keep `INotificationProvider` behavior stable for compatibility.
5. Prefer explicit logging for stream and delivery failures.

### Setup
```bash
cd funlab-sse
poetry install
```

---

## 正體中文

### 插件功能
- 透過 `GET /sse/<event_type>` 提供即時事件推送。
- 與 root notification 路由整合：
	- `GET /notifications/poll`
	- `POST /notifications/clear`
	- `POST /notifications/dismiss`
- 以資料庫 `EventEntity` 支援未讀事件回補與狀態維護。

### 核心目錄
- `funlab/sse/service.py`：插件服務與通知提供者整合。
- `funlab/sse/manager.py`：事件佇列、分派、回補、清理。
- `funlab/sse/model.py`：事件模型與持久化實體。

### 開發最佳實踐
1. 新功能僅修改正式路徑（`service.py` / `manager.py` / `model.py`）。
2. 避免在 ORM 層對其他 plugin table 建立強耦合。
3. 資料表結構演進應走 migration 流程，不在 runtime 透過跨 plugin 匯入硬解。
4. 維持 `INotificationProvider` 介面行為相容。
5. 對串流與投遞失敗提供可追蹤日誌。

### 安裝
```bash
cd funlab-sse
poetry install
```
