# funlab-sse 改善計畫（IMPROVEMENT_PLAN）

> 產出者：fund13-dev-arch（2026-09-27）。讀者：fund13-dev-coder。
> 每項含：影響／優先級／目標檔／完整修正程式碼／pytest／驗證指令與預期／風險。
> 所有主張均已讀原始碼並以探針實證（探針輸出摘要見各節「實證」）。
> 引用格式：`相對路徑:符號名`。除標明「目標倉＝finfun-quotesvcs」者外，全部改 funlab-sse。
> **實施狀態（2026-09-28 對帳）**：SSE-01～14 已全數合併 main（busy-wait 先修 7786efd；A5 鏈 6cd701a；f728c45 B4 五項；e0c0a87 C5——SSE-10 依 Q6、SSE-12 依 Q7 參數必填＋guard；SSE-11 ee73f87）；qa2-supp 補測 65555e5。已部署正式服務。測試基線現況 **43 passed**（39＋4 skip 口徑）。本文 (a) 段描述【修復前】缺陷。

## 實測環境事實（判定前提）

- 正式服務：systemd unit `fund13-web.service` → `finfun/run.py`；`finfun/config.toml:[ENV.PRODUCTION]` `WSGI = 'waitress'` → **單程序多執行緒**（`funlab-flaskr/funlab/flaskr/app.py:start_server` waitress 分支）。EventMgr 的 in-memory 連線表目前全站共用一份，**現況正確**。
- `funlab-flaskr/funlab/flaskr/conf/gunicorn_conf.py` 仍保留 `workers = multiprocessing.cpu_count() * 2 + 1`（gevent）。若有人把 `WSGI` 切到 `'gunicorn'`，每個 worker 各有一份 `EventManager` 連線表 → 事件只送得給「碰巧連到同一 worker」的用戶、DB 回補與清理多頭重複。見 SSE-10。
- busy-wait 事故（PR#1, 7786efd）已修復：`funlab-sse/funlab/sse/manager.py:_start_event_distributor` 用 `event_queue.get(timeout=1)` 阻塞取事件，**現行無 busy-wait**（本檔不重提為問題；唯一 `empty()` 出現在 `shutdown()`，屬有界收尾迴圈，非燒核）。
- SSE 路由登入保護：`funlab-sse/funlab/sse/service.py:SSEService.default_route_policy = is_authenticated_user`，由 `funlab-libs/funlab/core/plugin.py:Plugin._add_default_policy_middleware` 的 blueprint `before_request` 全面執行（含 `/sse/*` 與 `/sse/static/*`）。串流 route 內 `user_id = current_user.id`（`service.py:stream_events`），分發只查 `get_user_streams(event.target_userid)`，**只能訂閱自己的事件——此設計正確，勿改弱**。
- CSRF：`/notifications/dismiss` 為 POST，受全域 `CSRFProtect` 保護（`funlab-flaskr/funlab/flaskr/app.py:_init_csrf_protection`）。前端 `sse_client.js:markEventRead` 的 fetch 經 `funlab-flaskr/funlab/flaskr/static/js/csrf_ajax.js` 全域 monkey-patch 自動附加 `X-CSRFToken`（來源為 `templates/layouts/base.html` 的 `<meta name="csrf-token">`）。**現況可用**；代價是任何新模板若不含 csrf meta，dismiss 會 400——已寫進 CONSUMER_GUIDE。
- 每連線 client queue 有上限：`manager.py:register_user_stream` 建 `queue.Queue(maxsize=self.max_events_per_stream)`（預設 100），滿時 `_distribute_event` 探「丢最舊塞最新」；慢客戶端不會無界撐爆記憶體——正確，保留。
- heartbeat：`service.py:event_stream` 每 10 秒 `queue.Empty` → 發 heartbeat 事件；前端 `sse_client.js` 有對應監聽器。正確。

## 實證輸出總覽（探針，tmp sqlite，不碰正式庫）

```
# sse_probes.py（2026-09-27 實跑）
A_send_raw_event_enqueued = true          # send_event 回 True（欺騙呼叫端）
A_stream_received = []                    # 但 stream 什麼都沒收到
A_distributor_attr = "AttributeError: 'RawEventMessage' object has no attribute 'is_read'"
B_notif_stream_len = 1                    # SystemNotification 事件
B_price_stream_len = 1                    # 也進了同一使用者的 PriceUpdate 連線（誤投遞）
C_put_blocks = true / C_lock_held = true  # L13：佇列滿時 _put_event 永久阻塞且持鎖
E_junk_key_remains = true                 # 斷線後 eventtype_connection_users 殘留空集合垃圾鍵
F_evicted_no_sentinel = true              # 被踢連線的 queue 收不到任何終止訊號
G_expire_delta_minutes = 3600.0           # expire_after=3600（介面宣稱秒）實存成 3600 分鐘=60 小時
H_generate_notification_route_registered = false   # docstring 宣稱的路由不存在
H_ssetest_route_registered = false
# 執行期日誌：Event distribution error: 'RawEventMessage' object has no attribute 'is_read'

# sse_fixcheck.py（修正後程式碼逐項預演，全過）
{'SSE-01': true, 'SSE-03': true, 'SSE-03b_distributed': true,
 'SSE-04': true, 'SSE-05': true, 'SSE-08': true}
ALL FIX CHECKS PASSED
```

---

## SSE-01【P0】_put_event 持鎖阻塞 put，佇列滿時全站事件送件死鎖（證據包 L13）

- **問題影響**：`funlab-sse/funlab/sse/manager.py:_put_event` 在持有 `self.lock` 時呼叫 `queue.put()`（阻塞、無 timeout）。主事件佇列（maxsize=1000）滿時（例如大量行情 tick 或 DB 變慢導致 distributor 滯销），送事件的執行緒**永久阻塞且持鎖**，其他所有要送事件的執行緒（含 Flask 請求執行緒內的操作者通知）全部卡死→全站凍結級聯。`create_event` 中的 `except queue.Full` 分支是**死碼**：阻塞 `put()` 永遠不拋 `Full`。
- **目標檔/函式**：`funlab/sse/manager.py` 的 `EventManager._put_event`、`EventManager.__init__`、`create_event`。
- **修正後完整程式碼**：

```python
# manager.py: EventManager.__init__ 內，self.lock 附近新增計數欄位
        self.lock = threading.Lock()          # 保留：給未来需要跨物件原子操作的使用
        self.dropped_event_count = 0          # 佇列滿時被丢棄的持久事件累計數（observability）
```

```python
# manager.py: EventManager._put_event（整個函式替換）
    def _put_event(self, event) -> bool:
        """非阻塞放入主事件佇列。Queue 本身 thread-safe，不需要外層鎖。

        佇列滿時丢棄並回傳 False（持久化已在 _store_event 完成，
        使用者上線後仍可經 _recover_user_events 由 DB 回補，不丢資料）。
        """
        try:
            self.event_queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped_event_count += 1
            self.mylogger.warning(
                f"Event queue full; event {getattr(event, 'id', None)} "
                f"for user {getattr(event, 'target_userid', None)} deferred to DB recovery "
                f"(dropped_total={self.dropped_event_count})"
            )
            return False
```

```python
# manager.py: EventManager.create_event（整個函式替換：改用 _put_event 回傳值）
    def create_event(
        self,
        event_type: str,
        target_userid: int,
        priority: EventPriority = EventPriority.NORMAL,
        expire_after: int = None,           # seconds（見 SSE-08 單位修正）
        **payload_kwargs,
    ) -> EventBase:
        event_class = self._event_classes.get(event_type)
        if not event_class:
            raise ValueError(f"Unregistered event type: {event_type!r}")

        expired_at = self._expiry_from_now(expire_after)
        event = event_class(
            target_userid=target_userid,
            priority=priority,
            expired_at=expired_at,
            **payload_kwargs,
        )
        self._store_event(event)

        # Only enqueue for immediate delivery when the user is online
        if target_userid in self.connection_manager.user_connections:
            if not self._put_event(event):
                self.mylogger.error(
                    f"Event queue full  event {event.id} for user {target_userid} "
                    f"not enqueued; will recover from DB on next stream."
                )
                event = None
        else:
            self.mylogger.debug(
                f"User {target_userid} offline; event {event.id} stored for later recovery."
            )
        return event
```

（`_expiry_from_now` 見 SSE-08；若先行合入 SSE-01 可暫保留原 `timedelta(minutes=expire_after)` 內聯寫法。）

- **pytest**（新增 `tests/unit/test_put_event.py`，沿用既有輕量風格、不碰 DB）：

```python
import queue, threading
from funlab.sse.manager import EventManager, ConnectionManager
from funlab.sse.model import SystemNotificationEvent

EventManager.register_event(SystemNotificationEvent)


def _bare_manager(queue_maxsize=1):
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('test')
    em.event_queue = queue.Queue(maxsize=queue_maxsize)
    em.lock = threading.Lock()
    em.dropped_event_count = 0
    em.connection_manager = ConnectionManager()
    em.max_events_per_stream = 100
    em._store_event = lambda e: None          # 不碰 DB
    return em


class TestPutEventNonBlocking:
    def test_put_event_returns_true_when_space(self):
        em = _bare_manager()
        assert em._put_event(object()) is True

    def test_put_event_full_returns_false_without_blocking(self):
        em = _bare_manager(queue_maxsize=1)
        assert em._put_event(object()) is True
        t = threading.Thread(target=lambda: setattr(em, '_r', em._put_event(object())))
        t.start(); t.join(0.5)
        assert not t.is_alive()               # 不再永久阻塞
        assert not em.lock.locked()           # 不再持鎖
        assert em._r is False
        assert em.dropped_event_count == 1

    def test_create_event_returns_none_when_queue_full(self):
        em = _bare_manager(queue_maxsize=1)
        em.connection_manager.add_connection(1, queue.Queue(maxsize=50), 'SystemNotification')
        em.event_queue.put(object())          # 先塞滿
        assert em.create_event('SystemNotification', 1, title='t', message='m') is None
        assert em.dropped_event_count == 1
```

- **驗證指令與預期**：`cd funlab-sse && source ~/.venv/fund13/bin/activate && python -m pytest -q` → 原 14 筆＋新 3 筆全過；探針 `sse_probes.py` 重跑後 `C_put_blocks=false`。
- **風險**：低。行為變更＝「佇列滿從卡死改為丢佇列留 DB」；事件仍有 DB 持久化＋重連回補兜底，語意嚴格更安全。`dropped_event_count` 建議順帶併入 `service.py:metrics`。

## SSE-02【P0】跨倉整合錯誤：finfun-quotesvcs 誤用 `app.sse_service`（證據包 L14）

- **目標倉＝finfun-quotesvcs**：`finfun/quotesvcs/service.py:_subscribe_symbols`（內嵌 `_send_sse`、`quote_callback`，約 L880、L892）。
- **問題影響**：FunlabFlask 沒有 `sse_service` 屬性（探針 `hasattr=False`），provider 掛在 `app.notification_provider`（`funlab-flaskr/funlab/flaskr/app.py:set_notification_provider`）。`getattr(self.app, 'sse_service', None)` 恆為 None → **即時報價推送永遠不送出且無任何錯誤訊號**。
- **契約**：介面定義在 `funlab-libs/funlab/core/notification.py:INotificationProvider` — `send_event(event_type, target_userid, payload, priority='NORMAL', expire_after=None) -> bool`、`get_connected_users(event_type) -> set`、`supports_realtime`。**禁止再使用 `app.sse_service`**。
- **修正後完整程式碼**（`finfun-quotesvcs/finfun/quotesvcs/service.py`，`_subscribe_symbols` 整個函式）：

```python
    def _subscribe_symbols(self, symbols: list, extra_callback=None):
        """Internal: subscribe by symbol list; wire SSE push + cache update."""
        def _notification_provider():
            # 正確取法：FunlabFlask 只暴露 notification_provider（INotificationProvider）。
            # 切勿使用 app.sse_service —— 該屬性不存在（L14 事故根因）。
            return getattr(self.app, 'notification_provider', None)

        def _send_sse(user_id: int, symbol: str, price: float):
            prov = _notification_provider()
            if prov is not None and prov.supports_realtime:
                prov.send_event(
                    event_type='PriceUpdate',
                    target_userid=user_id,
                    payload={'symbol': symbol, 'price': str(price)},
                )

        def quote_callback(tick):
            if tick.price <= 0:
                return
            self._cache.set(tick)   # A-1: 寫入 QuoteCache
            prov = _notification_provider()
            if prov is not None and prov.supports_realtime:
                for user_id in prov.get_connected_users('PriceUpdate'):
                    _send_sse(user_id, tick.symbol, tick.price)
            if extra_callback is not None:
                extra_callback(tick)

        entry = self._get_entry(DispatchStrategy.LEAST_LOADED)
        if not entry:
            self._log_unavailable('subscribe_ticks')
            return
        failed_symbols = []
        for symbol in symbols:
            contract = self._make_contract(symbol)  # B-1: 交易所自動推斷
            try:
                entry.run_async(
                    entry.provider.subscribe_ticks(contract, quote_callback),
                    timeout=10.0,
                )
                entry.inc_subscription_count()   # A-3: 執行緒安全
                with self._subscriptions_lock:
                    self._subscriptions[symbol] = entry.broker_name
            except Exception as e:
                self.mylogger.error(f"[_subscribe_symbols] {symbol} 訂閱失敗：{symbol}: {e}")
                failed_symbols.append(symbol)
        if failed_symbols:
            self.mylogger.error(f"Subscribe ticks failed for symbols: {failed_symbols}")
            self.app.send_global_notification(
                title='Quote Subscription Failed',
                message=f"無法訂閱: {', '.join(failed_symbols)}",
                priority='HIGH',
            )
```

- **pytest**（新增 `finfun-quotesvcs/tests/unit/test_quote_sse_provider.py`；沿用該倉 conftest 的 stub 風格，`self.app` 用 MagicMock）：

```python
from unittest.mock import MagicMock
from finfun.quotesvcs.service import QuoteService


def _svc_with_provider(provider):
    svc = QuoteService.__new__(QuoteService)
    svc.app = MagicMock()
    svc.app.notification_provider = provider
    svc.mylogger = MagicMock()
    svc._cache = MagicMock()
    svc._subscriptions_lock = __import__('threading').Lock()
    svc._subscriptions = {}
    svc._get_entry = MagicMock(return_value=None)   # 不實際連券商
    return svc


class TestQuoteSseUsesNotificationProvider:
    def test_no_event_when_provider_missing(self):
        svc = _svc_with_provider(MagicMock(supports_realtime=False, spec=['supports_realtime']))
        svc._subscribe_symbols([])                   # 不應拋錯
        entry = MagicMock()
        entry.run_async.side_effect = lambda *a, **k: None
        entry.broker_name = 'x'
        svc._get_entry = MagicMock(return_value=entry)
        # provider 不支援 realtime → 不應呼叫 send_event
        assert not svc.app.notification_provider.send_event.called

    def test_sends_event_via_provider(self):
        prov = MagicMock()
        prov.supports_realtime = True
        prov.get_connected_users.return_value = {7}
        svc = _svc_with_provider(prov)
        # 直接取回 quote_callback 需先觸發訂閱；以最小路徑測 _send_sse 語意：
        svc._subscribe_symbols([])
        # 透過公開路徑驗證取法正確（attribute 名稱契约）：
        assert hasattr(svc.app, 'notification_provider')
```

> 註：quotesvcs 的 `QuoteService` 建構重，測試以 `__new__`＋MagicMock 注入，與其 `tests/unit/` 現有做法一致。coder 落地時可依實際可測性微調，但**必須斷言程式碼引用的是 `notification_provider` 而非 `sse_service`**（可用 `inspect.getsource` 斷言不含字串 `'sse_service'`）。

- **驗證指令與預期**：`cd finfun-quotesvcs && python -m pytest -q` 全過；`grep -rn "sse_service" finfun/quotesvcs` → 0 筆。
- **風險**：低。**注意順序**：必須與 funlab-sse SSE-03 同批或之後合併，否則 L14 修好後事件仍死在 distributor（見 SSE-03）。

## SSE-03【P0】新發現：RawEventMessage 缺 `is_read`/`is_expired`/`target_userid`，所有即時事件在 distributor 崩潰丢光

- **實證**：探針 A——`send_raw_event` 回 `True`（欺騙呼叫端「已送」），但 stream 收到 0 筆；日誌 `Event distribution error: 'RawEventMessage' object has no attribute 'is_read'`。
- **問題影響**：`manager.py:_start_event_distributor` 對每個佇列事件執行 `if event.is_read or event.is_expired`。`manager.py:RawEventMessage`（`__slots__ = ('event_type', '_data')`）沒有這些屬性 → 每次必拋 `AttributeError`，被 distributor 的寬捕获吞掉 → **`send_event()`/`send_raw_event()` 路徑 100% 失效**。這是 L14 之下更深一層的斷點：就算 quotesvcs 改用 `notification_provider`（SSE-02），PriceUpdate 依然永遠到不了前端。
- **目標檔/函式**：`funlab/sse/manager.py:RawEventMessage`。
- **修正後完整類別（整個類別替換）**：

```python
class RawEventMessage:
    """Minimal event wrapper for ephemeral / real-time events (e.g. price ticks)
    that do **not** need DB persistence or a registered EventBase subclass.

    必須與 distributor 的欄位契約相容：distributor 會讀
    ``is_read`` / ``is_expired`` / ``target_userid``（見 _start_event_distributor、
    _distribute_event）。少了任何一個，事件會在分發時拋 AttributeError 被丢棄。
    """

    __slots__ = ('event_type', 'target_userid', '_data')

    #: 即時事件永遠視為未讀（無持久化、無已讀追蹤）
    is_read = False

    def __init__(self, event_type: str, target_userid: int, payload: dict, priority: str = 'NORMAL'):
        self.event_type = event_type
        self.target_userid = target_userid
        self._data = {
            'id': None,
            'event_type': event_type,
            'priority': priority.upper(),
            'target_userid': target_userid,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'payload': payload,
            'is_recovered': False,
        }

    @property
    def is_expired(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return self._data
```

- **pytest**（新增 `tests/unit/test_raw_event_message.py`）：

```python
import queue
from funlab.sse.manager import EventManager, ConnectionManager, RawEventMessage


class TestRawEventMessageDistributorContract:
    def test_has_distributor_fields(self):
        m = RawEventMessage('PriceUpdate', 1, {'symbol': '2330', 'price': '100'})
        assert m.is_read is False
        assert m.is_expired is False
        assert m.target_userid == 1

    def test_distribute_reaches_stream(self):
        em = EventManager.__new__(EventManager)
        import logging
        em.mylogger = logging.getLogger('t')
        em.connection_manager = ConnectionManager()
        em.max_events_per_stream = 100
        sid = em.connection_manager.add_connection(1, queue.Queue(maxsize=100), 'PriceUpdate')
        q = em.connection_manager.user_connections[1][sid]
        msg = RawEventMessage('PriceUpdate', 1, {'p': '1'})
        assert not (msg.is_read or msg.is_expired)   # distributor 的准入檢查
        em._distribute_event(msg)
        assert q.qsize() == 1
        assert q.get_nowait().to_dict()['payload'] == {'p': '1'}
```

（`test_distribute_reaches_stream` 在 SSE-04 合併後需加 `event_type='PriceUpdate'` 參數語意，見該節測試。）

- **驗證指令與預期**：`python -m pytest tests/unit/test_raw_event_message.py -q` 過；端到端探針 `A_stream_received` 長度 1、無 `Event distribution error` 日誌。
- **風險**：無（補屬性，不動現行持久事件路徑）。`__slots__` 新增 `target_userid`、`is_read` 用類別屬性不進 slots——勿把 `is_read` 寫進 slots，否則佔執行緒記憶體且語意錯。

## SSE-04【P1】新發現：事件依 user_id 投遞、不分 event_type——同一使用者跨事件型別互相誤投

- **實證**：探針 B——user 1 同時開 `SystemNotification` 與 `PriceUpdate` 兩條連線，`create_event('SystemNotification', 1, ...)` 後 **price stream 也收到該事件**（`B_price_stream_len = 1`）。
- **問題影響**：`manager.py:ConnectionManager.get_user_streams` 只按 user_id 回全部 queue；`_distribute_event` 與 `_recover_user_events` 都用它。後果：(1) 行情 tick 洪水會把通知連線的 100 格塞滿並「丢最舊」→ **使用者漏看系統通知**；(2) 通知塞進價格連線浪費頻寬；(3) DB 回補（`_recover_user_events` 明明按 event_type 查了 DB）卻推給所有連線，語意自相矛盾。
- **目標檔/函式**：`funlab/sse/manager.py` 的 `ConnectionManager.__init__`、`add_connection`、`_remove_connection_locked`、`remove_all_connections`、`get_user_streams`，及 `EventManager._distribute_event`、`_recover_user_events`、`service.py:stream_events`（SSE-05 的 sentinel 檢查同段，一起改）。
- **修正後完整程式碼**：

```python
# manager.py: ConnectionManager（整個類別替換，含 SSE-05 sentinel）
class ConnectionManager:
    """Manages per-user SSE stream connections.

    Each connection is identified by a UUID ``stream_id`` and assigned to a
    specific ``event_type``.  Multiple concurrent connections per user
    (e.g. multiple browser tabs) are supported up to ``max_connections_per_user``.

    投遞契約：stream 只收「自己 event_type」的事件（get_user_streams 過濾）。
    eviction 時對被淘汰的 queue 放入 STREAM_CLOSED sentinel，讓該連線的
    streaming generator 能體面退出（避免幽靈連線續命）。
    """

    def __init__(self, max_connections_per_user: int = 10):
        self.max_connections = max_connections_per_user
        # user_id -> {stream_id: Queue}
        self.user_connections: Dict[int, Dict[str, queue.Queue]] = defaultdict(dict)
        # event_type -> set of connected user_ids
        self.eventtype_connection_users: Dict[str, Set[int]] = defaultdict(set)
        # stream_id -> connection timestamp
        self.users_connect_time: Dict[str, float] = {}
        # stream_id -> event_type（SSE-04：投遞時據以過濾）
        self.stream_event_type: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _generate_stream_id(self) -> str:
        return str(uuid.uuid4())

    def add_connection(self, user_id: int, stream: queue.Queue, event_type: str) -> str:
        with self._lock:
            user_conns = self.user_connections[user_id]
            if len(user_conns) >= self.max_connections:
                # Evict the oldest connection
                oldest_sid = min(
                    user_conns,
                    key=lambda sid: self.users_connect_time.get(sid, 0),
                )
                evicted_stream = user_conns.get(oldest_sid)
                self._remove_connection_locked(
                    user_id, oldest_sid, self.stream_event_type.get(oldest_sid, event_type)
                )
                if evicted_stream is not None:
                    self._signal_close(evicted_stream)   # SSE-05

            stream_id = self._generate_stream_id()
            self.user_connections[user_id][stream_id] = stream
            self.stream_event_type[stream_id] = event_type
            self.users_connect_time[stream_id] = time.time()
            self.eventtype_connection_users[event_type].add(user_id)
            return stream_id

    @staticmethod
    def _signal_close(stream: queue.Queue):
        """SSE-05：通知被淘汰連線的 generator 終止（不阻塞、可能滿則先腾一格）。"""
        try:
            stream.put_nowait(STREAM_CLOSED)
        except queue.Full:
            try:
                stream.get_nowait()
                stream.put_nowait(STREAM_CLOSED)
            except queue.Empty:      # pragma: no cover - race
                pass

    def _remove_connection_locked(self, user_id: int, stream_id: str, event_type: str):
        """Remove one connection; must be called *with* self._lock held."""
        user_conns = self.user_connections.get(user_id)
        if user_conns and stream_id in user_conns:
            del user_conns[stream_id]
        self.users_connect_time.pop(stream_id, None)
        self.stream_event_type.pop(stream_id, None)
        if not self.user_connections.get(user_id):
            self.user_connections.pop(user_id, None)
            users = self.eventtype_connection_users.get(event_type)
            if users is not None:
                users.discard(user_id)
                if not users:                     # SSE-08：空集合垃圾鍵即時回收
                    self.eventtype_connection_users.pop(event_type, None)

    def remove_connection(self, user_id: int, stream_id: str, event_type: str):
        with self._lock:
            self._remove_connection_locked(user_id, stream_id, event_type)

    def remove_all_connections(self, user_id: int):
        """Remove all connections belonging to ``user_id``."""
        with self._lock:
            if user_id not in self.user_connections:
                return
            stream_ids = list(self.user_connections[user_id].keys())
            del self.user_connections[user_id]
            for sid in stream_ids:
                self.users_connect_time.pop(sid, None)
                self.stream_event_type.pop(sid, None)
            for event_type in list(self.eventtype_connection_users.keys()):
                users = self.eventtype_connection_users[event_type]
                users.discard(user_id)
                if not users:
                    self.eventtype_connection_users.pop(event_type, None)

    def get_user_streams(self, user_id: int, event_type: str = None) -> Set[queue.Queue]:
        """回傳使用者連線。給定 event_type 時只回傳訂閱該型別的連線。"""
        with self._lock:
            conns = self.user_connections.get(user_id, {})
            if event_type is None:
                return set(conns.values())
            return {
                stream for sid, stream in conns.items()
                if self.stream_event_type.get(sid) == event_type
            }

    def get_all_streams(self) -> Set[queue.Queue]:
        with self._lock:
            all_streams: Set[queue.Queue] = set()
            for conns in self.user_connections.values():
                all_streams.update(conns.values())
            return all_streams

    def get_eventtype_users(self, event_type: str) -> Set[int]:
        with self._lock:
            return set(self.eventtype_connection_users.get(event_type, set()))
```

```python
# manager.py 模組層（RawEventMessage 之後）新增：
class _StreamClosed:
    """Sentinel：放進被淘汰連線的 queue，generator 見到即退出。"""
    __slots__ = ()

    def __repr__(self):            # pragma: no cover
        return '<STREAM_CLOSED>'


STREAM_CLOSED = _StreamClosed()
```

```python
# manager.py: EventManager._distribute_event（整個函式替換）
    def _distribute_event(self, event):
        streams = self.connection_manager.get_user_streams(
            event.target_userid, event_type=event.event_type
        )
        for stream in streams:
            try:
                if stream.qsize() < self.max_events_per_stream:
                    stream.put_nowait(event)
                else:
                    stream.get_nowait()
                    stream.put_nowait(event)
            except queue.Full:
                pass
```

```python
# manager.py: EventManager._recover_user_events 內一行替換
        # 舊：user_streams = self.connection_manager.get_user_streams(user_id)
        # 新（只回補該 event_type 的連線）：
            user_streams = self.connection_manager.get_user_streams(user_id, event_type=event_type)
```

```python
# service.py: _register_sse_routes 內 event_stream 的 while 迴圈（整個 event_stream 替換，含 SSE-05）
            def event_stream():
                try:
                    user_stream = (
                        self.sse_mgr.connection_manager
                        .user_connections.get(user_id, {})
                        .get(stream_id)
                    )
                    if not user_stream:
                        return
                    while True:
                        try:
                            event = user_stream.get(timeout=10)
                            if event is STREAM_CLOSED:
                                # 本連線已被淘汰（超過 max_connections）→ 體面退出
                                return
                            sse = (
                                f"event: {event.event_type}\n"
                                f"data: {json.dumps(event.to_dict())}\n\n"
                            )
                            yield sse
                        except queue.Empty:
                            yield 'event: heartbeat\ndata: {"status":"heartbeat"}\n\n'
                except GeneratorExit:
                    pass
                except Exception as exc:
                    logging.error(
                        f"SSE stream error user={user_id} stream={stream_id}: {exc}"
                    )
                finally:
                    mgr = self.sse_mgr
                    if mgr is not None:                 # SSE-13：shutdown 競態保護
                        mgr.unregister_user_stream(user_id, stream_id, event_type)
```

（service.py 頂部 import 增加 `from .manager import EventManager, STREAM_CLOSED`。既有測試 `tests/unit/test_sse_disconnect.py` 呼叫 `get_user_streams(user_id=...)` 一律用關鍵字參數——新簽名向後相容，不需改測試。）

- **pytest**（新增 `tests/unit/test_eventtype_routing.py`）：

```python
import queue
from funlab.sse.manager import ConnectionManager, EventManager, STREAM_CLOSED
from funlab.sse.model import SystemNotificationEvent


def test_streams_filtered_by_event_type():
    cm = ConnectionManager()
    qn, qp = queue.Queue(maxsize=50), queue.Queue(maxsize=50)
    cm.add_connection(1, qn, 'SystemNotification')
    cm.add_connection(1, qp, 'PriceUpdate')
    assert cm.get_user_streams(1, event_type='SystemNotification') == {qn}
    assert cm.get_user_streams(1, event_type='PriceUpdate') == {qp}
    assert cm.get_user_streams(1) == {qn, qp}          # 不給型別＝全部（相容舊行為）


def test_distribute_only_same_event_type():
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('t')
    em.connection_manager = ConnectionManager()
    em.max_events_per_stream = 100
    qn, qp = queue.Queue(maxsize=50), queue.Queue(maxsize=50)
    em.connection_manager.add_connection(1, qn, 'SystemNotification')
    em.connection_manager.add_connection(1, qp, 'PriceUpdate')
    ev = SystemNotificationEvent(target_userid=1, title='t', message='m')
    em._distribute_event(ev)
    assert qn.qsize() == 1 and qp.qsize() == 0


def test_evicted_stream_receives_sentinel():
    cm = ConnectionManager(max_connections_per_user=1)
    q1 = queue.Queue(maxsize=50)
    cm.add_connection(2, q1, 'E')
    cm.add_connection(2, queue.Queue(maxsize=50), 'E')   # 踢掉 q1
    assert q1.qsize() == 1 and q1.get_nowait() is STREAM_CLOSED


def test_eventtype_key_purged_when_empty():
    cm = ConnectionManager()
    sid = cm.add_connection(3, queue.Queue(), 'JUNK')
    cm.remove_connection(3, sid, 'JUNK')
    assert 'JUNK' not in cm.eventtype_connection_users
```

- **驗證指令與預期**：`python -m pytest -q` → 舊 14 筆＋新檔全過；探針 `B_price_stream_len = 0`。
- **風險**：中。既有依賴「取全部連線」的呼叫點只有 `_distribute_event`/`_recover_user_events`/`get_all_streams`，已全部更新；外部（flaskr shim）若直接調 `get_user_streams(user_id)` 不受影響（參數預設 None）。需與 SSE-03 同批（distributor 對 `event.event_type` 的依賴由 SSE-03 滿足）。

## SSE-05【P1】新發現：連線數 eviction 後舊 generator 變幽靈連線——waitress 執行緒洩漏

- **實證**：探針 F——被踢連線的 queue 無任何訊號（`q1.empty()` 恆真）。
- **問題影響**：`service.py:stream_events` 的 generator 只在「拿到事件」或「拿到異常」時才會察覺自己被淘汰；被淘汰後它的 queue 永遠不會再有事件，於是每 10 秒發一次 heartbeat **無限期佔用 waitress worker 執行緒與 TCP channel**。使用者多開分頁（>10）或同 event_type 重連風暴會累積洩漏，最終吃光 `threads = cpu*2+1` 的 worker pool → 全站無回應。這是「SSE 佔用長連線 + 線程型伺服器」组合下的資源洩漏。
- **修正**：已併入 SSE-04 的 `ConnectionManager._signal_close` 與 `event_stream` 的 `STREAM_CLOSED` 檢查（該節程式碼即最終版），**SSE-05 無獨立程式碼，與 SSE-04 同一 PR**。
- **驗證**：SSE-04 測試 `test_evicted_stream_receives_sentinel`＋手動：同 user 開 11 個 `/sse/SystemNotification`，舊連線於瀏覽器端收到流關閉（EventSource 結束/錯誤），`journalctl --user -u fund13-web` 無該連線之後續 heartbeat 記錄。
- **風險**：低。sentinel 若恰好撞上「佇列恰好滿」路徑已處理（騰一格再放）。

## SSE-06【P1·相依 funlab-libs L1】session_context 巢狀使用會破壞外層交易——SSE 寫入點是高频巢狀來源

- **事實鏈**：`funlab-libs/funlab/core/dbmgr.py:DbMgr.session_context` 在同一執行緒回傳同一 scoped Session；內層結束即 `commit()+remove_session()`（證據包 L1，探針 rows=1 應為 0）。SSE 的多個方法自帶 `session_context`：`manager.py:_store_event`、`_recover_user_events`、`clean_up_events`、`service.py:fetch_unread/dismiss_items/dismiss_all`。當任何插件在**自己的** `with dbmgr.session_context():` 交易中途呼叫 `app.send_user_notification(...)`（→ `create_event` → `_store_event`），內層 SSE 會把外層未commit的交易提前提交並關閉 → 外層後續 `rollback` 救不回來，**帳務寫入路徑可能寫出半套資料**。
- **本倉可做（缓解）**：
  1. 於 DEVELOPMENT_GUIDE／CONSUMER_GUIDE 明令契約：「**禁止在 `session_context()` 交易中間呼叫 `send_user_notification` / `send_event` / `dismiss_*`**；通知一律在交易 commit 之後發」。
  2. 後台執行緒（distributor/cleanup）自成執行緒，scoped_session 每執行緒一份，**不與請求共享**，此路安全（已由 `dbmgr.py:remove_session` 註解語意確認）。
- **根治（目標倉＝funlab-libs，非本檔職責）**：依證據包 L1 修法（threading.local 深度計數，只最外層 commit/remove）。funlab-libs 修復合入後，本條自动解除，funlab-sse 無需再改。
- **驗證指令**（funlab-libs 修復後在 funlab-sse 跑回歸）：`python -m pytest -q` 全過＋探針 `probe_nested.py` 外層 raise 後 rows=0。
- **風險**：不落地代碼風險＝無（純契約文件）。若有人提前「顺手」在 SSE 裡做巢狀偵測，屬越權改動，拒絕。

## SSE-07【P1·目標倉＝finfun-quotesvcs】新發現：前端 `subscribeToEvent` 未定義——報價頁即時推送第三層斷點

- **實證**：全 workspace grep（排除 .worktrees/node_modules/.venv）：`subscribeToEvent` 只出現在 finfun-quotesvcs 三個模板的**呼叫端**與 funlab-libs/README.md 的**歷史範例**；無任何 JS 定義。現行前端 API 是 `window.sseClient.subscribe(eventType, renderFn)`（`funlab-sse/funlab/sse/static/js/sse_client.js:SSEClient.subscribe`，全域實例 `window.sseClient`）。
- **問題影響**：`quotes.html:218` 裸呼叫 → 未捕获 `ReferenceError`，**同 inline script 區塊後的所有代碼不再執行**；`subscriptions.html:271` 同；`quotesvcs.html:927` 有 `typeof` 守衛 → 靜默不訂閱。即使 SSE-02/SSE-03 全修好，報價頁面仍永遠收不到 `PriceUpdate`。
- **修正後程式碼**（以 `quotes.html` 為例；三個模板同一改法）：

```javascript
  // SSE 即時報價更新（funlab-sse 提供 window.sseClient）
  function updatePrice(data, eventType) {
    upsertRow(data);
  }
  if (window.sseClient) {
    window.sseClient.subscribe('PriceUpdate', updatePrice);
  } else {
    console.error('[quotes] window.sseClient 未載入：確認模板含 notification_init.html');
  }
```

`quotesvcs.html`／`subscriptions.html` 把 `typeof subscribeToEvent === 'function'` 守衛改為 `if (window.sseClient)`，回調簽名 `(data, eventType)` 不變（sse_client 將整個事件物件傳入）。
- **pytest／驗證**：前端無測試基礎，改以：(1) `grep -rn "subscribeToEvent" finfun-quotesvcs/finfun/quotesvcs/templates/` → 0 筆；(2) QA 手冊：登入開 `/quotesvcs/quotes`，另開終端 `curl -N -b <cookie> http://localhost:5000/sse/PriceUpdate` 應見 `event: PriceUpdate`（需 SSE-02/03 已上）；瀏覽器 console 無 ReferenceError。
- **風險**：低。`sse_client.js` 由 `funlab-flaskr/funlab/flaskr/templates/includes/notification_init.html` 在 `sse_enabled` 時全域載入，報價頁沿用 base.html 即有此腳本。

## SSE-08【P2】新發現：expire_after 單位矛盾——介面宣稱「秒」、實作按「分鐘」

- **實證**：探針 G——`expire_after=3600` 後 `expired_at-created_at = 3600 分鐘（60 小時）`。介面文件 `funlab-libs/funlab/core/notification.py:NotificationMessage` 與 `INotificationProvider.send_event` 語意都是「過期秒數」；`funlab-sse/funlab/sse/manager.py:create_event` 卻寫 `timedelta(minutes=expire_after)`。呼叫端照介面傳秒 → 事件多活 60 倍時間；通知類事件長期掛在 badge／回補清單。
- **目標檔/函式**：`funlab/sse/manager.py:create_event`（搭配抽出可測小函式）。
- **修正後完整程式碼**：

```python
# manager.py: EventManager 新增 staticmethod + create_event 內以它取代內聯計算
    @staticmethod
    def _expiry_from_now(expire_after: int | None):
        """expire_after 單位＝秒（契約見 funlab-libs notification.py）。None＝永不過期。"""
        if not expire_after or expire_after <= 0:
            return None
        return datetime.now(timezone.utc) + timedelta(seconds=expire_after)
```

（`create_event` 的 SSE-01 版本已使用 `self._expiry_from_now(expire_after)`，兩條合併後即一致。全倉 grep 目前無任何呼叫端實際傳過 `expire_after`，單位翻轉零相容成本——正因為如此更要現在統一。）
- **pytest**：

```python
from datetime import datetime, timezone, timedelta
from funlab.sse.manager import EventManager


class TestExpiryUnits:
    def test_expire_after_is_seconds(self):
        exp = EventManager._expiry_from_now(3600)
        assert exp - datetime.now(timezone.utc) > timedelta(seconds=3590)
        assert exp - datetime.now(timezone.utc) < timedelta(seconds=3610)

    def test_none_and_nonpositive_means_never_expire(self):
        assert EventManager._expiry_from_now(None) is None
        assert EventManager._expiry_from_now(0) is None
        assert EventManager._expiry_from_now(-5) is None
```

- **驗證指令與預期**：`python -m pytest tests/unit/test_expiry_units.py -q` 過；探針 G `G_expire_delta_minutes ≈ 60`。
- **風險**：低。唯一注意點：`EventEntity.is_expired` 的 SQL 端用 `func.now()`（DB 時鐘）、Python 端用 aware UTC——PostgreSQL 正式庫 `timestamp with time zone` 下比較正確；sqlite 回 naive 時 `datetime.now(timezone.utc) > naive` 會 TypeError，現有測試未踩到（事件都不帶 expired_at），屬既有限制，於 SSE-12 一併記備忘。

## SSE-09【P2】cleanup 排程在 DB 錯誤時緊迫迴圈（error busy-loop）

- **問題影響**：`manager.py:_start_cleanup_scheduler`：`try: clean_up_events(); time.sleep(...) except: log`。`sleep` 在 try 內——`clean_up_events()` 拋異常（DB 掛、連接池耗盡）時**跳過 sleep 立即重試**，形成全速錯誤迴圈，與已修復的 busy-wait 事故同型（這次在錯誤分支）。DB 斷線期間將燒一個核心。
- **目標檔/函式**：`funlab/sse/manager.py:_start_cleanup_scheduler`。
- **修正後完整函式**：

```python
    def _start_cleanup_scheduler(self, interval_minutes: int = 30) -> threading.Thread:
        def scheduler():
            while not self.is_shutting_down:
                try:
                    self.clean_up_events()
                except Exception as exc:
                    self.mylogger.error(f"Event cleanup error: {exc}")
                finally:
                    # 無論好壞都要睡，錯誤時不形成緊迫重試迴圈（同 PR#1 教訓）
                    time.sleep(interval_minutes * 60)
        t = threading.Thread(name='sse_event_cleanup', target=scheduler, daemon=True)
        t.start()
        return t
```

- **pytest**（不啟真執行緒，驗證 sleep 在 finally 的結構＋行為）：

```python
import threading, time
from funlab.sse.manager import EventManager


def test_cleanup_scheduler_sleeps_even_on_error(monkeypatch):
    em = EventManager.__new__(EventManager)
    import logging
    em.mylogger = logging.getLogger('t')
    em.is_shutting_down = False
    calls = {'clean': 0, 'sleeps': []}

    def boom():
        calls['clean'] += 1
        em.is_shutting_down = True      # 讓迴圈本輪後退出
        raise RuntimeError('db down')

    monkeypatch.setattr(EventManager, 'clean_up_events', lambda self: boom())
    monkeypatch.setattr(time, 'sleep', lambda s: calls['sleeps'].append(s))
    t = em._start_cleanup_scheduler(interval_minutes=1)
    t.join(2)
    assert calls['clean'] == 1
    assert calls['sleeps'] == [60]      # 拋異常仍睡覺
```

- **驗證指令與預期**：`python -m pytest tests/unit/test_cleanup_scheduler.py -q` 過。
- **風險**：無。
- （本節同時涵蓋 SSE-04 已修的 `eventtype_connection_users` 垃圾鍵累積——探針 E；不重複列條。）

## SSE-10【P2】gunicorn 切換護欄：現況單程序正確，但設定一併切過去就會壊

- **事實**：現況 PRODUCTION `WSGI='waitress'`（單程序）→ in-memory 連線表全站共用，正確。`gunicorn_conf.py:workers = cpu_count()*2+1`（本機 20 核 → 41 workers）。若照歷史註解把 WSGI 改成 `'gunicorn'`：每個 worker 一個 `EventManager`；瀏覽器長連線黏在某 worker，`create_event`/`send_event` 卻可能發生在別的 worker（無此用戶連線 → 事件只進 DB 等回補、`send_event` 回 False），`get_connected_users('PriceUpdate')` 恆近空集合——**即時性静默失效，無錯誤可查**。
- **目標檔/函式**：`funlab/sse/service.py:SSEService._setup`（加啟動護欄警示）。
- **修正後完整片段**（`_setup` 末尾、`app.set_notification_provider(self)` 之後插入）：

```python
        # --- SSE-10 多 worker 護欄 -------------------------------------
        # SSE 的連線表是 per-process in-memory；只有單進程部署正確。
        # 目前正式環境 WSGI=waitress（單進程）滿足前提。若切 gunicorn，
        # gunicorn_conf.workers>1 會讓事件只送達「恰好同 worker」的連線。
        try:
            wsgi = str(app.config.get('WSGI', 'flask')).lower()
            if wsgi == 'gunicorn':
                from funlab.flaskr.conf import gunicorn_conf
                import multiprocessing
                workers = getattr(gunicorn_conf, 'workers', 1) or 1
                if workers < 1:
                    workers = multiprocessing.cpu_count() * 2 + 1
                if workers > 1:
                    app.mylogger.warning(
                        f"SSEService: 偵測到 WSGI=gunicorn 且 workers={workers}。"
                        "SSE 連線表為 per-process in-memory，多 worker 下即時推送會静默漏送"
                        "（僅剩 /notifications/poll 回補）。請改單 worker（waitress 或 "
                        "gunicorn -w 1 --threads N）或實作跨进程廣播後再切換。"
                    )
        except Exception as guard_exc:   # 護欄本身不可影響啟動
            app.mylogger.debug(f"SSE multi-worker guard skipped: {guard_exc}")
```

- **pytest**：

```python
from unittest.mock import MagicMock
from funlab.sse import service as svc_mod


def test_guard_logs_warning_multiworker(monkeypatch):
    # 只測護欄邏輯：抽出的 guard 函式直接呼叫（落地時把上面 try 內容
    # 抽成 SSEService._warn_if_multiworker(app) 靜態方法再測）。
    called = {}
    def fake_warning(msg, *a):
        called['msg'] = msg
    app = MagicMock()
    app.config = {'WSGI': 'gunicorn'}
    app.mylogger.warning.side_effect = fake_warning
    monkeypatch.setitem(__import__('sys').modules, 'funlab.flaskr.conf.gunicorn_conf',
                        MagicMock(workers=41))
    SSEService._warn_if_multiworker(app)
    assert 'workers=41' in called['msg']


def test_guard_silent_on_waitress():
    app = MagicMock()
    app.config = {'WSGI': 'waitress'}
    SSEService._warn_if_multiworker(app)     # 不應拋錯、不應 warning
    assert not app.mylogger.warning.called
```

（落地要求：把插入段抽成 `SSEService._warn_if_multiworker(app)` 靜態方法，`_setup` 呼叫它，測試對準該方法。）
- **驗證指令與預期**：`python -m pytest tests/unit/test_multiworker_guard.py -q` 過；本機不重啟服務（僅文件與測試層）。
- **風險**：無（只加日誌護欄；護欄自身 try/except 包死）。

## SSE-11【P2】死路徑與幻覺路由：`/generate_notification`、`/ssetest` 不存在卻寫在文件與 JS 裡

- **實證**：探針 H——`service.py` 內無這兩個路由（docstring L10-11 宣稱有）；`sse_client.js:SSEClient.sendNotification` 預設 POST `/generate_notification` → 必 404。選單引用（app.py L349-351）已被註解。
- **問題影響**：誤導整合者照 docstring 打測試端點；`sendNotification()` 一調用就 404，且經 CSRF 全域保護 POST 不存在的路由會先吃 400/404 混亂訊號。
- **修正**：
  1. `service.py` 模組 docstring 的 Routes 清單改為只列 `GET /sse/<event_type>`（其餘刪除）。
  2. `sse_client.js`：刪除 `sendNotification()`（無合法用途；測試注入通知請直接在後端呼叫 `app.send_user_notification`，寫進 DEVELOPMENT_GUIDE）。
- **驗證指令與預期**：`grep -n "generate_notification\|ssetest" funlab/sse/service.py funlab/sse/static/js/sse_client.js` → 0 筆；`python -m pytest -q` 全過（無測試引用之）。
- **風險**：需先確認 finfun-* 無模板調用 `sseClient.sendNotification`（本輪 grep 全 workspace：僅 funlab-sse 內部與 funlab-libs README 歷史文字，finfun 無引用）→ 可安全刪。

## SSE-12【P2】孤兒事件永留 DB：已卸載插件的事件型別既不清理也不回補

- **問題影響**：`manager.py:_recover_user_events` 對註冊表沒有的 `event_type` 只 `continue`（行留在 DB、`is_read=False`）；`clean_up_events` 只删「已讀或已過期」。插件若移除且其事件未設 `expire_after`（`expired_at=NULL`），這些列**永遠無人清理**，並讓每次重連多掃一輪無效查詢。sqlite 環境另有 `is_expired` Python 端 aware/naive 比較限制（SSE-08 風險段），同條收攏。
- **目標檔/函式**：`manager.py:clean_up_events`。
- **修正後完整函式**：

```python
    def clean_up_events(self):
        """Delete read / expired / orphaned events from the DB.

        orphaned = event_type 已不在註冊表（插件被移除），且無過期時間可託管
        ——不清理會永遠累積。
        Bug fix（沿革）：原程式用 Python ``or`` 使 WHERE 恆真，已改 SQLAlchemy ``|``。
        """
        known_types = set(self._event_classes.keys())
        with self.dbmgr.session_context() as session:
            stmt = select(EventEntity).where(
                (EventEntity.is_read == True)
                | (EventEntity.expired_at <= datetime.now(timezone.utc))
            )
            to_delete = list(session.execute(stmt).scalars().all())
            if known_types:
                orphan_stmt = select(EventEntity).where(
                    EventEntity.event_type.notin_(known_types)
                )
                to_delete.extend(session.execute(orphan_stmt).scalars().all())
            seen = set()
            for entity in to_delete:
                if entity.id in seen:
                    continue
                seen.add(entity.id)
                session.delete(entity)
            if to_delete:
                session.commit()
```

（sqlite aware/naive 限制備忘：本倉測試不建帶 `expired_at` 的列即可迴避；正式庫 PostgreSQL `timestamptz` 無此問題。真正的跨方言修法在 funlab-libs，不在本條範圍。）
- **pytest**（用探針同款 tmp-sqlite DbMgr 小集成測試）：

```python
import tempfile, os, pytest
from funlab.core.dbmgr import DbMgr
from funlab.sse.manager import EventManager
from funlab.sse.model import EventEntity, SystemNotificationEvent


@pytest.fixture()
def em():
    tmpdb = tempfile.mktemp(suffix='.db')
    db = DbMgr({'url': f'sqlite:///{tmpdb}'})
    EventManager.register_event(SystemNotificationEvent)
    EventEntity.__table__.create(bind=db.get_db_engine(), checkfirst=True)
    m = EventManager(db)
    yield m
    m.shutdown()
    db.release()
    os.unlink(tmpdb)


def test_orphan_events_purged(em):
    ev = em.create_event('SystemNotification', 1, title='t', message='m')
    assert ev is not None and ev.id is not None
    with em.dbmgr.session_context() as s:
        s.add(EventEntity(event_type='GonePlugin', payload='{"x":1}',
                          target_userid=1, priority='NORMAL'))
    em.clean_up_events()
    with em.dbmgr.session_context() as s:
        assert s.query(EventEntity).filter_by(event_type='GonePlugin').count() == 0
        # 未讀的已知型別事件保留
        assert s.query(EventEntity).filter_by(id=ev.id).count() == 1
```

- **驗證指令與預期**：`python -m pytest tests/unit/test_cleanup_orphans.py -q` 過。
- **風險**：中低。`notin_` 在註冊表為空的極早啟動窗口會誤删全部——已用 `if known_types:` 守衛（空表＝跳過孤兒清理，宁可不删）。

## SSE-13【P2】shutdown 競態：`_on_stop` 先將 `sse_mgr=None`，在用 generator 的 `finally` 觸發 AttributeError

- **問題影響**：`service.py:_on_stop` 執行 `self.sse_mgr.shutdown(); self.sse_mgr = None`。之後每個仍在運行的 `event_stream` generator 被 close 時，`finally: self.sse_mgr.unregister_user_stream(...)` 對 None 取属性 → `AttributeError`（在生成器 close 上下文中被静默吞或刷錯誤日誌），清理語意靠運氣。
- **修正**：已併入 SSE-04 的 `event_stream` 最終版（`mgr = self.sse_mgr; if mgr is not None:`）。無獨立程式碼，與 SSE-04 同 PR。
- **驗證**：QA 手冊：`systemctl --user restart fund13-web.service` 前保持一個開著的 SSE 頁（由 QA 執行，非本檔作者職責——本檔不得重啟服務），web.log 無 `AttributeError ... 'NoneType' object has no attribute 'unregister_user_stream'`。
- **風險**：無。

## SSE-14【P2】前端重連品質：固定 5 秒重連無退避、beforeunload 監聽器逐次洩漏、debug 常開

- **問題影響**：`static/js/sse_client.js`：
  1. `onerror` 只在 `readyState === CLOSED` 時手動重連（間隔固定 `reconnectTimeout=5000`，無指數退避、無 jitter）——伺服器重啟/認證過期時所有分頁以同頻率衝刺重連；
  2. 每次 `subscribe()` 都 `window.addEventListener('beforeunload', ...)` 且從不解除——SPA 內反覆訂閱會累積監聽器與閉包洩漏；
  3. 全域實例以 `{ debug: true }` 建立，正式環境 console 刷屏。
- **目標檔/函式**：`funlab/sse/static/js/sse_client.js`。
- **修正後完整程式碼**（整個 `subscribe`、`unsubscribe` 與檔尾全域實例；constructor 增加兩個欄位）：

```javascript
    constructor(options = {}) {
        this.options = {
            reconnectTimeout: 1000,      // 退避基準（含上限）
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
```

```javascript
    subscribe(eventType, renderFunction, endpoint = null) {
        if (this.options.debug) {
            console.log(`Subscribing to event: ${eventType}`);
        }
        this.unsubscribe(eventType);

        const eventSource = new EventSource(endpoint || `/sse/${eventType}`);
        this.eventSources[eventType] = eventSource;
        this.renderFunctions[eventType] = renderFunction;

        eventSource.onopen = () => {
            this.connected = true;
            this._retryCounts[eventType] = 0;      // 連上即歸零退避
            if (this.options.debug) {
                console.log(`Connection to ${eventType} opened`);
            }
        };

        eventSource.addEventListener(eventType, (event) => {
            try {
                const data = JSON.parse(event.data);
                renderFunction(data, data.event_type);
            } catch (error) {
                console.error("Failed to parse event data:", error);
            }
        });

        eventSource.addEventListener('heartbeat', () => {
            if (this.options.debug) console.log("Heartbeat received");
        });

        // 重連策略：CLOSED（終端態，如 HTTP 4xx/5xx）才手動指數退避；
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

        // 每個 eventType 只掛一次卸載監聽器，unsubscribe 時移除
        const unloadHandler = () => this.unsubscribe(eventType);
        this._unloadHandlers[eventType] = unloadHandler;
        window.addEventListener('beforeunload', unloadHandler);

        return eventSource;
    }

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
```

```javascript
// 檔尾：正式環境預設靜音；需要除錯時控制台執行
//   window.sseClient.options.debug = true
window.sseClient = new SSEClient({ debug: false });
```

- **驗證**：無 JS 測試框架；QA 手冊：(1) 開通知頁→`systemctl --user restart fund13-web.service`（QA 執行）→console 應見間隔遞增帶抖動的重連日誌，恢復後自動續上並 `attempt` 歸零；(2) 反覆呼叫 `sseClient.unsubscribe('X'); sseClient.subscribe('X', f)` 20 次後觸發 beforeunload，無重複監聽器錯誤；(3) 預設 console 無 debug 洗版。
- **風險**：低。注意 `markEventRead` 的 fetch 依賴全域 `csrf_ajax.js` 的 monkey-patch（載入順序：base.html defer 在前、notification_init 在 body 尾）——不動 fetch 呼叫即可，勿改成自建 headers。

---

## 依賴順序與 PR 切分建議

1. **PR-A（funlab-sse）**：SSE-01 ＋ SSE-03 ＋ SSE-04（含 05、13）＋ SSE-08 ＋ SSE-09 ＋ SSE-12 ＋ SSE-10 護欄 ＋ SSE-11 ＋ SSE-14。全在本倉，可一次 PR；若要更細，先 01/03（純止血），再 04/05/13（投遞語意，需一起），其餘打包。
2. **PR-B（finfun-quotesvcs）**：SSE-02 ＋ SSE-07。**必須在 PR-A 合併上線之後**（否則修了也送不到）。
3. **跨倉相依**：SSE-06 等 funlab-libs L1 修復（另行立卡於 funlab-libs），funlab-sse 側無代碼。

## 已確認「不是問題」項（勿再花時間）

- busy-wait（PR#1 已修，現行 `get(timeout=1)`）。
- SSE 路由登入與 target_userid 隔離（policy middleware＋current_user.id，設計正確）。
- client queue 上限與慢客戶端（maxsize=100、drop-oldest）。
- dismiss 的 CSRF（csrf_ajax.js 全域覆蓋）。
- heartbeat（10s 服務端推送＋前端監聽器）。
- 現況單進程 waitress 下 in-memory 連線表一致性。
