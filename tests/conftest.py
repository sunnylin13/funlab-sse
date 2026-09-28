import sys

# registry stub（歷史遺留防護）：早期 funlab.core.appbase 把 APP_ENTITIES_REGISTRY
# 和整個 Flask 棧綁在一起，單測 import entity 會拖進多模組依賴。
# funlab-libs 已把 registry 隔離到 funlab.core._entity_registry（僅依賴
# sqlalchemy.orm），優先用真 registry（SSE-06 巢狀 session 測試需要真實
# __table__ 映射）；僅在真模組不可 import 時退回舊 pass-through stub。
try:
    import funlab.core._entity_registry  # noqa: F401
except Exception:
    from unittest.mock import MagicMock

    class _RegistryStub:
        def mapped(self, cls):
            return cls

    _entity_registry_module = MagicMock()
    _entity_registry_module.APP_ENTITIES_REGISTRY = _RegistryStub()
    sys.modules['funlab.core._entity_registry'] = _entity_registry_module
