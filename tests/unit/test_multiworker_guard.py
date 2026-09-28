"""SSE-10◆Q6：WSGI=gunicorn 且 workers>1 的啟動護欄警示（IMPROVEMENT_PLAN SSE-10）。

PM 裁示（2026-09-28）：續用 waitress（單進程前提），不切 gunicorn。
本護欄只加日誌警示，護欄自身 try/except 包死，不影響啟動。
"""
from unittest.mock import MagicMock

from funlab.sse.service import SSEService


def test_guard_logs_warning_multiworker(monkeypatch):
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


def test_guard_never_raises_when_flaskr_missing(monkeypatch):
    # funlab-flaskr 不可导入時護欄必須靜默跳過（不得拖垮啟動）
    import sys
    app = MagicMock()
    app.config = {'WSGI': 'gunicorn'}
    monkeypatch.setitem(sys.modules, 'funlab.flaskr.conf.gunicorn_conf', None)
    SSEService._warn_if_multiworker(app)     # import None 模組 → 例外 → 內部吞掉
