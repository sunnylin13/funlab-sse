import sys
from unittest.mock import MagicMock

# registry stub: APP_ENTITIES_REGISTRY.mapped is a pass-through decorator
class _RegistryStub:
    def mapped(self, cls):
        return cls

_entity_registry_module = MagicMock()
_entity_registry_module.APP_ENTITIES_REGISTRY = _RegistryStub()
sys.modules['funlab.core._entity_registry'] = _entity_registry_module