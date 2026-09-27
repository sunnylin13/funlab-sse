"""Q7 未裁示的 guard 附註測試：send_user_notification(target_userid=None)

Q7（介面說廣播、實作寫入 None 孤列）尚未裁示。本卡紅線：**不改 None 語意**，
只做誠實化附註——以下測試把「現行 None 行為」釘成回歸契約：
None 仍原樣委派 create_event（寫 DB 孤列、不廣播、不丟例外、不拒寫）。
未來 Q7 裁示要改語意（必填＋guard 或真廣播）時，必須刻意改寫本測試。
"""
import logging
from unittest.mock import MagicMock

from funlab.sse.service import SSEService


def _bare_service():
    svc = SSEService.__new__(SSEService)
    app = MagicMock()
    app.mylogger = logging.getLogger('t')
    svc.app = app
    svc.sse_mgr = MagicMock()
    svc.sse_mgr.create_event.return_value = object()
    return svc


class TestNoneTargetSemanticsFrozen:
    def test_none_target_still_delegates_to_create_event(self):
        svc = _bare_service()
        result = svc.send_user_notification('t', 'm', target_userid=None)
        kwargs = svc.sse_mgr.create_event.call_args.kwargs
        assert kwargs['target_userid'] is None
        assert result is not None          # 語意不變：照常回傳事件物件

    def test_int_target_delegates_unchanged(self):
        svc = _bare_service()
        svc.send_user_notification('t', 'm', target_userid=5)
        kwargs = svc.sse_mgr.create_event.call_args.kwargs
        assert kwargs['target_userid'] == 5

    def test_sse_mgr_none_returns_none_honestly(self):
        svc = _bare_service()
        svc.sse_mgr = None
        assert svc.send_user_notification('t', 'm', target_userid=5) is None
