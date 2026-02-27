# SSE Plugin 模組使用與開發指引（funlab-sse）

## 1. 目標與適用範圍
本文件為 `funlab-sse` 的單一整合指引，涵蓋：
- 使用方式（安裝、啟用、基本操作）
- 模組架構與執行流程
- 開發擴充與維護準則
- 測試與除錯建議

> 目前正式執行路徑：`service.py` → `manager.py` → `model.py`。

---

## 2. 快速開始

### 2.1 安裝
在 monorepo 環境建議使用可編輯安裝：

```bash
cd funlab-sse
poetry install
```

### 2.2 Plugin 載入
`pyproject.toml` 已宣告 entry point：
- group: `funlab_plugin`
- plugin: `SSEService = "funlab.sse.service:SSEService"`

主程式啟動後，`ModernPluginManager` 會自動探索並載入。

### 2.3 路由與互動介面
- 串流路由：`GET /sse/<event_type>`
- 通知聚合路由（由 root blueprint 提供）：
  - `GET /notifications/poll`
  - `POST /notifications/clear`
  - `POST /notifications/dismiss`

---

## 3. 模組架構

### 3.1 `SSEService`（`funlab/sse/service.py`）
責任：
- 註冊 `SystemNotificationEvent`
- 初始化 `EventManager`
- 設定自己為 `notification_provider`
- 提供 `INotificationProvider` 介面方法（send/fetch/dismiss）
- 提供 SSE stream route 並處理 heartbeat

生命週期重點：
- 啟動：`_setup()` 建表 + 啟動 manager
- 停止：`_on_stop()` 呼叫 `EventManager.shutdown()`

### 3.2 `EventManager`（`funlab/sse/manager.py`）
責任：
- 建立與分派事件（in-memory queue）
- 使用者重連時回補未讀事件（DB recovery）
- 週期性清理 read/expired 事件
- 管理 distributor / cleanup 背景執行緒

### 3.3 `EventEntity / EventBase`（`funlab/sse/model.py`）
責任：
- `EventEntity`：資料庫持久化模型（table: `event`）
- `EventBase`：記憶體事件物件與序列化
- `SystemNotificationEvent`：內建事件型別

設計原則：
- `target_userid` 為整數欄位，不在 ORM mapper 強綁其他 plugin table
- `is_expired` 具備 hybrid expression，可安全用於 SQL 條件

---

## 4. 使用範例

### 4.1 發送單一使用者通知
由 app 端呼叫（委派給 provider）：

```python
app.send_user_notification(
    title="Build Complete",
    message="Your report is ready",
    target_userid=123,
    priority="NORMAL",
)
```

### 4.2 發送全域通知

```python
app.send_global_notification(
    title="System Notice",
    message="Maintenance tonight 23:00",
    priority="HIGH",
)
```

### 4.3 前端接收
前端需連線到：
- `/sse/SystemNotification`

並處理：
- `event: <event_type>` payload
- heartbeat（避免閒置斷線誤判）

---

## 5. 開發最佳實踐

1. **只改正式路徑**：新功能優先加在 `service.py` / `manager.py` / `model.py`。
2. **避免跨 plugin 強耦合**：不要在 mapper 加上外部 plugin 的 ORM FK。
3. **資料庫變更走 migration**：schema 演進透過 Alembic/部署流程，不在 runtime 偷做相依匯入。
4. **保持介面相容**：維持 `INotificationProvider` 行為一致，避免破壞 root routes。
5. **執行緒安全優先**：修改 queue / connection 結構時，先確認 lock 範圍與關閉流程。
6. **可觀測性**：錯誤至少記錄 user_id、event_type、stream_id（若有）。

---

## 6. 測試建議

### 6.1 手動驗證
1. 登入並開啟通知頁
2. 呼叫 `send_user_notification`
3. 確認 SSE 即時到達
4. 關閉頁面後產生通知，再重開頁面，確認可由 poll/recovery 回補
5. 測試 `dismiss` / `clear` 是否正確更新狀態

### 6.2 自動化方向
- 單元測試：
  - `EventBase` ↔ `EventEntity` round-trip
  - `is_expired` 條件查詢
- 整合測試：
  - stream 建立與 heartbeat
  - reconnect recovery
  - dismiss API 對 DB 狀態的影響

---

## 7. 文件與檔案治理

- 本文件為 SSE 開發主要指引。
- 歷史原型已下放到 `funlab/sse/legacy/`，不應在新開發直接引用。
- 架構調整時，請同步更新本文件與 README 連結。
