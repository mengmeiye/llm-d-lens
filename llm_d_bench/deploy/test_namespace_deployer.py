from __future__ import annotations

from pathlib import Path

import pytest

from llm_d_bench.deploy.namespace_deployer import (
    discover_guide_overlays,
)

pytestmark = pytest.mark.no_database


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "llm-d"
    overlay = source / "guides" / "optimized-baseline" / "modelserver" / "gpu" / "vllm" / "base"
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    router = source / "guides" / "optimized-baseline" / "router"
    router.mkdir()
    (router / "optimized-baseline.values.yaml").write_text("router: {modelName: example/model}\n", encoding="utf-8")
    recipes = source / "guides" / "recipes" / "router"
    recipes.mkdir(parents=True)
    (recipes / "base.values.yaml").write_text("global: {}\n", encoding="utf-8")
    (source / "guides" / "optimized-baseline" / "guide.yaml").write_text(
        """
support:
  engines: {vllm: vLLM}
  accelerators:
    gpu:
      model: example/model
      engines: {vllm: validated}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return source


def test_discovers_upstream_overlay_and_support_metadata(tmp_path):
    catalog = discover_guide_overlays(_source(tmp_path))

    assert len(catalog) == 1
    assert catalog[0].guide == "optimized-baseline"
    assert catalog[0].guide_label == "Optimized Baseline"
    assert catalog[0].accelerator == "gpu"
    assert catalog[0].accelerator_label == "GPU"
    assert catalog[0].engine == "vllm"
    assert catalog[0].engine_label == "vLLM"
    assert catalog[0].variant == "base"
    assert catalog[0].variant_label == "Base"
    assert catalog[0].model == "example/model"
    assert catalog[0].maturity == "validated"
    assert catalog[0].supported is True


def test_catalog_uses_guide_accelerator_and_provider_labels(tmp_path):
    source = _source(tmp_path)
    (source / "guides" / "optimized-baseline" / "guide.yaml").write_text(
        """
name: optimized-baseline
support:
  engines: {vllm: vLLM}
  accelerators:
    gpu:
      label: NVIDIA GPU
      model: example/model
      providers:
        - {name: base, label: "Kubernetes (base)"}
      engines: {vllm: validated}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    [entry] = discover_guide_overlays(source)

    assert entry.guide_label == "Optimized Baseline"
    assert entry.accelerator_label == "NVIDIA GPU"
    assert entry.engine_label == "vLLM"
    assert entry.variant_label == "Kubernetes (base)"


def test_catalog_keeps_undeclared_guide_overlay_disabled(tmp_path):
    source = _source(tmp_path)
    (source / "guides" / "optimized-baseline" / "guide.yaml").write_text("name: optimized-baseline\n", encoding="utf-8")

    [entry] = discover_guide_overlays(source)

    assert entry.supported is False
    assert entry.reason == "Guide support matrix is unavailable"
