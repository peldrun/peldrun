# backend/peldrun/llm/catalog/__init__.py
"""
PELDRUN Model Catalog.

External model capability discovery via curated registries. Provides
cross-reference data from:

    * LiteLLM community registry (``external_registry.py``) — cloud
      and local models, capability facts curated by the community.
    * HuggingFace model cards (``huggingface.py``) — local models,
      vision/audio presence derived from the model author's config.

The merge layer (``lookup.py``) combines these sources with the
metadata vault, respecting the priority order defined in the plan §4.1
and exposing a per-field source marker so the UI can honestly report
where each fact came from.
"""

from peldrun.llm.catalog.external_registry import (
    ExternalModelRegistry,
    RegistryEntry,
    get_external_model_registry,
)
from peldrun.llm.catalog.huggingface import (
    HFModelConfig,
    HuggingFaceConfigLoader,
    get_huggingface_config_loader,
)

__all__ = [
    "ExternalModelRegistry",
    "RegistryEntry",
    "get_external_model_registry",
    "HFModelConfig",
    "HuggingFaceConfigLoader",
    "get_huggingface_config_loader",
]
