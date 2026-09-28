# funlab-sse 開發指南（DEVELOPMENT GUIDE）

適用對象：開發/維護 `funlab-sse` 本身的工程師。如何「使用」模組請看 [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md)。

## 1. 執行路徑與架構

單一正式路徑：`funlab/sse/service.py` → `manager.py` → `model.py`（無 legacy 目錄）。

| 元件 | 檔案:符號 | 職責 |
|---|---|---|
| SSEService | `funlab/sse/service.py:SSEService` | ServicePlugin ＋ INotificationProvider；建表、註冊路由、生命週期 |
| EventManager | `funlab/sse/manager.py:EventManager` | 事件建立/持久化、主佇列＋distributor 執行緒、DB 回補、週期清理 |
| ConnectionManager | `funlab/sse/manager.py:ConnectionManager` | `user_id -> {stream_id: Queue}` 連線表、event_type 索引、上限淘汰 |
| RawEventMessage | `funlab/sse/manager.py:RawEventMessage` | 非持久即時事件的輕量包裝 |
| EventBase / EventEntity | `funlab/sse/model.py` | 記憶體事件 ↔ 表 `event`（SQLAlchemy dataclass mapping，共用 `APP_ENTITIES_REGISTRY`） |
| SystemNotificationEvent | `funlab/sse/model.py` | 內建事件型別（payload: `title`,`message`） |

執行緒模型（全部 daemon 執行緒，per-process）：
- **distributor**（`manager.py:_start_event_distributor`）：`event_queue.get(timeout=1)` 阻塞輪詢 → `_distribute_event` 投到目標使用者各 stream。（busy-wait 事故已由 PR#1 修復；改動此迴圈時**嚴禁**改回 `empty()` 輪詢。）
- **cleanup**（`manager.py:_start_cleanup_scheduler`）：每 30 分鐘 `clean_up_events()`（錯誤迴圈缺陷見 IMPROVEMENT_PLAN SSE-09）。
- 每個 SSE 請求佔一條 waitress 執行緒直到斷線（長連線＋線程伺服器的既有成本）。

路由：
- `GET /sse/<event_type>`：本插件 blueprint（url_prefix `/sse`），經 `default_route_policy = is_authenticated_user`（`funlab-libs/funlab/core/plugin.py:Plugin._add_default_policy_middleware` 的 before_request 統一執行）。靜態檔 `/sse/static/js/*` 同受政策保護。
- `/notifications/*`（poll/clear/dismiss）由 funlab-flaskr root blueprint 註冊，request 時經 `current_app.notification_provider` 分發——改 provider 介面即會波及這些路由，保持 `INotificationProvider` 契約不動。

生命週期：
- 啟動：`__init__` → `_setup()`：註冊內建事件類別 → `EventEntity.__table__.create(checkfirst=True)` → `EventManager(dbmgr)` → 註冊路由 → `app.set_notification_provider(self)`。
- 每請求 teardown **不動** SSE（`_teardown` 故意 pass——它是常駐服務，勿「顺手」加 shutdown）。
- 停止：`unload() → stop() → _on_stop()`：`EventManager.shutdown()`（持久化佇列內未送事件 → 斷開所有連線 → join 兩條執行緒 → 清理）。重載：`_on_reload()` 重建 EventManager 並重掛 provider。

## 2. 定義自訂持久事件（custom persistent events）

持久事件＝有註冊 EventBase 子類、寫入表 `event`、支援斷線回補的事件。機制全在 `funlab/sse/model.py` ＋ `manager.py:EventManager.register_event`。

### 2.1 三段式定義（照 SystemNotificationEvent 的既有模式）

```python
# 在你的插件套件內（範例：finfun-xxx/finfun/xxx/events.py）
from dataclasses import dataclass
from funlab.sse.model import EventBase, PayloadBase
from funlab.sse.manager import EventManager


@dataclass
class OrderFilledPayload(PayloadBase):          # 1) payload：強型別 dataclass
    symbol: str
    qty: int
    price: float


@dataclass(init=False)                          # 2) 事件類別：@dataclass(init=False) 必寫
class OrderFilledEvent(EventBase):              #    類名必須以 Event 結尾
    payload: OrderFilledPayload                 #    payload 型別註解＝序列化依據（必填）


EventManager.register_event(OrderFilledEvent)   # 3) 註冊（幂等；重複註冊覆蓋）
```

規則（全部源自現行碼，違反必炸）：
- **event_type 由類名去掉尾碼 `Event` 推得**：`OrderFilledEvent → 'OrderFilled'`（`manager.py:register_event` 與 `model.py:EventBase.__init__` 都用 `removesuffix('Event')`）。前端就以這個字串訂閱 `/sse/OrderFilled`。
- `@dataclass(init=False)` 不可漏：EventBase 自訂 `__init__`（`model.py:EventBase.__init__`），讓 dataclass 再生成 init 會蓋掉它。
- `payload` 欄位**必須是型別註解**（`get_type_hints(cls)['payload']` 是反序列化唯一來源）；欄位值放進 payload dataclass，不要放事件層。
- Payload 欄位需 JSON-safe（`PayloadBase.to_json()` 用 `json.dumps(self.__dict__)`；datetime 等要先轉字串）。
- 註冊時機：插件 `_setup()`/`__init__` 期內、任何 `create_event` 呼叫前。回補與 poll 會跳過未註冊型別（`manager.py:_recover_user_events` 只 warning）——**卸載插件前未讀事件會滯留 DB**，清理由 IMPROVEMENT_PLAN SSE-12 的孤兒清理接手。

### 2.2 發送與消費

```python
# 後端發送（持久；線上即時送、離線回補）
event = app.notification_provider.sse_mgr.create_event(   # 或經 app.send_user_notification
    'OrderFilled', target_userid=42, priority='HIGH',
    symbol='2330', qty=1000, price=512.5,
)  # 回傳 EventBase（含 DB id）；佇列滿時 None（事件仍在 DB，重連回補）

# 前端
window.sseClient.subscribe('OrderFilled', (data) => { /* data.payload.symbol ... */ });
```

（若不想持有 `sse_mgr`，建議在插件 service 內包一個方法呼叫；不建議把 `create_event` 加進 `INotificationProvider`——那會波及 PollingNotificationProvider。）

### 2.3 schema 變遷

表 `event` 的 `payload` 是 JSON 欄：**payload 加欄位不需 migration**（`from_jsonstr` 以 `cls(**data)` 還原 → 新欄位要有預設值，否則舊資料反序列化 TypeError——這是既有序列化的硬契約）。刪/改名欄位＝破壞舊列，必須先清空或遷移 `event` 表。改表結構走 Alembic，勿在 runtime 加 DDL。

## 3. 非持久即時事件

`INotificationProvider.send_event` → `manager.py:send_raw_event`：不經註冊表、不入 DB、離線回 False。適合高頻 tick。注意欄位契約（distributor 讀 `is_read`/`is_expired`/`target_userid`）與現況缺陷 IMPROVEMENT_PLAN SSE-03。

## 4. 開發守則

1. **只改 `service.py`/`manager.py`/`model.py` 正式路徑**；funlab-flaskr 內的 `sse/` shim 僅轉匯入本倉。
2. **不給 `EventEntity.target_userid` 加 ORM 級 FK**——跨插件隔離設計（`model.py:EventEntity` 註解），引用完整性交給 migration 層。
3. **鎖紀律**：`ConnectionManager` 全部公開方法自帶 `_lock`；不要在持鎖期間做阻塞 I/O（教訓＝IMPROVEMENT_PLAN SSE-01/證據包 L13）。
4. **distributor / cleanup 的等待必須有 timeout 或 sleep**，錯誤分支也要退避（PR#1＋SSE-09 兩次的共同教訓）。
5. session 使用：SSE 各方法用 `dbmgr.session_context()`，但**呼叫端可能已在交易中**（巢狀即壊外層事務）——對外函式文件要寫「勿在他人 session_context 內呼叫」（SSE-06）。
6. 錯誤日誌至少帶 user_id、event_type、stream_id。
7. `metrics`（`service.py:SSEService.metrics`）已含 connected_users/streams/event_queue_size；新增計數（如 dropped_event_count）請併入，供 /health 與 plugin 面板使用。

## 5. 測試

- 現況：`tests/unit/test_sse_disconnect.py` 14 筆（純 ConnectionManager，`conftest.py` stub 掉 `APP_ENTITIES_REGISTRY`）。
- 跑法：`cd funlab-sse && source ~/.venv/fund13/bin/activate && python -m pytest -q`。
- 方向（IMPROVEMENT_PLAN 各條已附可貼測試）：`_put_event` 非阻塞、RawEventMessage 契約、event_type 投遞過濾、expire 單位、cleanup 錯誤退避、孤兒清理；集成層（tmp sqlite DbMgr）測 `clean_up_events` 與回補。
- 手動端到端（QA）：登入 → `curl -N -b <cookie> http://localhost:5000/sse/SystemNotification` ＋另一終端觸發通知；關頁再送、重開看回補；dismiss 後 poll 為空。

## 6. 部署前提與容量

- **單進程 WSGI**（現況 waitress，`threads = cpu*2 + 1`）。切 gunicorn 多 worker 會使連線表 per-process 分裂——啟動護欄見 IMPROVEMENT_PLAN SSE-10。
- 每連線佔一條伺服器執行緒；`ConnectionManager.max_connections=10`/user、client queue `max_events_per_stream=100`（drop-oldest）；主佇列 `max_event_queue_size=1000`。設定檔覆寫點：`funlab/sse/conf/plugin.toml [SSEService]`（經 `Plugin._init_configuration` 併入 app config）。
- ⚠️ 現況註記：`SSEService._setup` 建立 `EventManager(app.dbmgr)` 未帶 plugin.toml 參數——檔頭註解宣稱可覆寫 `max_event_queue_size` 實際上不生效（固定走預設值）。屬 IMPROVEMENT_PLAN 之外的低優先代辦，動它時一併修。
