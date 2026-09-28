"""SSE-12◆Q7 裁示落地：send_user_notification(target_userid=None) 顯式拒絕。

本檔原本是「Q7 未裁示」的回歸凍結測試（釘住 None→create_event 寫孤列舊行為）。
PM 書面裁示（2026-09-28，C5 卡 t_ec356272）：Q7＝**參數必填＋guard**——
呼叫端不再傳 None，函式入口對 None 顯式拒絕（guard log＋回傳 None，
不寫孤列、不廣播）。依原檔自述的約定，本次為刻意改寫。
"""
import logging
from unittest.mock import MagicMock

import pytest

from funlab.sse.service import SSEService


def _bare_service():
    svc = SSEService.__new__(SSEService)
    app = MagicMock()
    app.mylogger = logging.getLogger('t')
    svc.app = app
    svc.sse_mgr = MagicMock()
    svc.sse_mgr.create_event.return_value = object()
    return svc


class TestNoneTargetRejected:
    def test_none_target_rejected_without_db_write(self, caplog):
        svc = _bare_service()
        with caplog.at_level(logging.WARNING, logger='t'):
            result = svc.send_user_notification('t', 'm', target_userid=None)
        assert result is None
        svc.sse_mgr.create_event.assert_not_called()   # 不寫孤列
        assert any('REJECTED' in r.message for r in caplog.records)  # guard log

    def test_int_target_delegates_unchanged(self):
        svc = _bare_service()
        svc.send_user_notification('t', 'm', target_userid=5)
        kwargs = svc.sse_mgr.create_event.call_args.kwargs
        assert kwargs['target_userid'] == 5

    def test_target_userid_positional_required(self):
        # Q7 必填語意：未傳 target_userid → TypeError（簽名層面鎖定）
        svc = _bare_service()
        with pytest.raises(TypeError):
            svc.send_user_notification('t', 'm')

    def test_sse_mgr_none_returns_none_honestly(self):
        svc = _bare_service()
        svc.sse_mgr = None
        assert svc.send_user_notification('t', 'm', target_userid=5) is None
