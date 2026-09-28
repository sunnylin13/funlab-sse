# funlab-sse 文件索引

> 全部內容已逐一核對 `funlab/sse/{service,manager,model}.py`、`funlab-libs/funlab/core/notification.py`、`funlab-flaskr/funlab/flaskr/app.py` 現行碼（2026-09-27）。

| 文件 | 用途 |
|---|---|
| [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) | **消費端**：如何發通知／推即時事件／前端訂閱／回補與已讀契約 |
| [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) | **開發端**：模組架構、自訂持久事件、生命週期、測試 |
| [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) | 待辦改善項 SSE-01…SSE-14（含完整修正程式碼與測試）；給 fund13-dev-coder |

## 快速事實卡

- 插件註冊：`pyproject.toml` entry point `funlab_plugin → SSEService = "funlab.sse.service:SSEService"`（`load_mode = "startup"`、`security_mode = "required"`）；uv workspace 安裝，**不是 poetry**。
- 串流路由：僅 `GET /sse/<event_type>`（需登入）。`/generate_notification`、`/ssetest` **不存在**（見 IMPROVEMENT_PLAN SSE-11）。
- 通知聚合路由由 funlab-flaskr root blueprint 提供：`GET /notifications/poll`、`POST /notifications/clear`、`POST /notifications/dismiss`（皆需登入；POST 需 `X-CSRFToken`）。
- 事件持久化：**有**，表 `event`（`funlab/sse/model.py:EventEntity`），由 `SSEService._setup()` `checkfirst=True` 建立； unread 回補靠 DB，不用 Last-Event-ID。
- 取用通知服務的唯一正確姿势：`app.notification_provider`（`app.send_user_notification` 等 app 層方法亦可）。**`app.sse_service` 不存在，禁止使用**。
- 部署前提：**單進程** WSGI（現況 waitress）。多 worker（gunicorn_conf 預設值）會使 in-memory 連線表失效，見 IMPROVEMENT_PLAN SSE-10。
