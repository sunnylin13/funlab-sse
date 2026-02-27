# SSE Plugin 使用者視角開發指引（Consumer Guide）

本文件從「使用此模組的應用開發者」角度說明如何正確整合 `funlab-sse`，而非插件內部實作細節。

## 1. 你會得到什麼
整合 `funlab-sse` 後，你的應用可獲得：
- 即時事件串流（SSE）
- 使用者離線期間事件回補（unread recovery）
- 與現有通知 API 相容的 provider 介面

## 2. 適合誰閱讀
- 需要在 FunlabFlask 應用中「發送通知」的功能開發者
- 維護前端通知 UI 的工程師
- 需要驗收 SSE 行為（即時/回補/已讀）的 QA 與 DevOps

## 3. 整合前檢查清單
1. 已安裝 plugin（`poetry install` 或等價流程）
2. 主程式 plugin manager 會自動載入 `SSEService`
3. 前端已建立 SSE 連線至 `/sse/SystemNotification`
4. 前端保留 fallback 流程（`/notifications/poll`）

## 4. 你應該怎麼使用（後端）

### 4.1 發送單一使用者通知
```python
app.send_user_notification(
    title="Task Completed",
    message="Your export is ready",
    target_userid=123,
    priority="NORMAL",
)
```

### 4.2 發送系統廣播通知
```python
app.send_global_notification(
    title="System Notice",
    message="Deployment at 23:00",
    priority="HIGH",
)
```

### 4.3 不要直接依賴內部細節
建議只透過 app/provider 介面呼叫，不直接耦合 `EventManager` 內部資料結構，避免升級破壞。

## 5. 你應該怎麼使用（前端）

### 5.1 連線端點
- `GET /sse/SystemNotification`

### 5.2 必要處理
- 正常事件：解析 payload 更新 UI
- heartbeat 事件：維持連線活性
- 斷線重連：採用指數退避（建議）
- 首次進頁或重連後：呼叫 `/notifications/poll` 做狀態對齊

## 6. 通知生命週期（Consumer 觀點）
1. 服務端建立事件並儲存
2. 線上使用者即時收到 SSE
3. 離線使用者於下次連線時回補 unread
4. 使用者操作 dismiss/clear 後，事件標記已讀

## 7. 常見整合錯誤
- 只做 SSE，不做 poll fallback，導致重連前漏顯示
- 直接把 UI 邏輯綁死某單一 event payload 格式
- 忽略已讀 API，造成通知重複出現
- 把 plugin 內部模組當公開 API（升級風險高）

## 8. 驗收建議（最小可行）
1. 連線後可即時收到新通知
2. 關閉頁面後送通知，重開可看到回補
3. dismiss 單筆後不再出現
4. clear all 後清空列表

## 9. 與內部開發指引的分工
- 本文件：**如何使用模組**（應用整合）
- `sse-plugin-development-guide.zh-TW.md`：**如何開發模組本身**（內部設計與維護）
