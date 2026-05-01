# funlab-sse

Server-Sent Events (SSE) 模組，提供即時事件推播至前端瀏覽器。

## 持久化策略

**決議**：SSE 事件不持久化（無 DB 儲存）。

理由：
- SSE 為即時通知，歷史回放需求低。
- 持久化增加複雜度，且前端斷線重連時可透過 Last-Event-ID 補發近期事件。
- 近期事件（最後 N 筆）保留於記憶體環形緩衝區，容量由 SSE_BUFFER_SIZE 控制（預設 100）。

### 斷線重連策略

1. 客戶端以 Last-Event-ID 標頭重連。
2. 伺服器端從環形緩衝區補發 Last-Event-ID 之後的事件。
3. 緩衝區中已不存在的事件不補發（客戶端需自行處理遺漏）。

## 事件分類

| 事件類型 | 說明 |
|---|---|
| 
otification | 系統通知（任務完成、錯誤警告等） |
| quote_update | 行情更新（即時報價） |
| order_update | 委託/成交更新 |

## 待實作（Wave 3）

- T-sse-002：斷線處理測試（Last-Event-ID 補發）
- T-sse-003：PluginManager 對齊（依賴 T-libs-003，已完成）
