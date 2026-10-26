"""Database-free, existing-namespace deployment lifecycle for llm-d Guides."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.core.exceptions import DomainValidationError


@dataclass(frozen=True)
class GuideOverlay:
    guide: str
    guide_label: str
    overlay: str
    accelerator: str
    accelerator_label: str
    engine: str
    engine_label: str
    variant: str
    variant_label: str
    model: str | None
    maturity: str | None
    supported: bool
    reason: str | None = None


def resolve_source_root(value: str | Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve an llm-d checkout without downloading or persisting it."""
    environment = environ or os.environ
    explicit = value or environment.get("LLM_D_ROOT")
    candidates = [Path(explicit).expanduser()] if explicit else [Path.cwd(), *Path.cwd().parents]
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "guides").is_dir():
            return root
    raise DomainValidationError("source must point to an llm-d checkout containing guides/")


def discover_guide_overlays(source_root: Path) -> list[GuideOverlay]:
    """Discover model-server Kustomize entry points from upstream Guide files."""
    guides_root = source_root / "guides"
    if not guides_root.is_dir():
        raise DomainValidationError("source must contain guides/")
    entries: list[GuideOverlay] = []
    for guide_root in sorted(path for path in guides_root.iterdir() if path.is_dir()):
        modelserver_root = guide_root / "modelserver"
        if not modelserver_root.is_dir():
            continue
        descriptor = _guide_descriptor(guide_root)
        support = descriptor.get("support") if isinstance(descriptor.get("support"), dict) else {}
        accelerator_support = support.get("accelerators") if isinstance(support.get("accelerators"), dict) else {}
        declared_engines = support.get("engines") if isinstance(support.get("engines"), dict) else {}
        guide_label = _display_identifier(str(descriptor.get("label") or descriptor.get("name") or guide_root.name))
        engine_names = set(declared_engines)
        for accelerator_entry in accelerator_support.values():
            if not isinstance(accelerator_entry, dict):
                continue
            accelerator_engines = accelerator_entry.get("engines")
            if isinstance(accelerator_engines, dict):
                engine_names.update(accelerator_engines)
        kustomizations = sorted(modelserver_root.rglob("kustomization.y*ml"))
        for kustomization in kustomizations:
            relative_modelserver = kustomization.parent.relative_to(modelserver_root)
            parts = relative_modelserver.parts
            accelerator, engine, variant = _overlay_coordinates(parts, engine_names)
            support_entry = accelerator_support.get(accelerator)
            model = support_entry.get("model") if isinstance(support_entry, dict) else None
            engines = support_entry.get("engines") if isinstance(support_entry, dict) else {}
            maturity = engines.get(engine) if isinstance(engines, dict) else None
            accelerator_label = _metadata_label(support_entry, accelerator)
            engine_label = _metadata_label(declared_engines.get(engine), engine)
            variant_label = _overlay_variant_label(support_entry, variant, engine)
            standard_values = _router_values_path(source_root, guide_root.name, engine)
            reason = None
            if "components" in parts or "common" in parts:
                reason = "overlay is an internal Kustomize component"
            elif any(
                other.parent != kustomization.parent and other.parent.is_relative_to(kustomization.parent)
                for other in kustomizations
            ):
                reason = "overlay is an internal Kustomize base"
            elif not accelerator_support or not engine_names:
                reason = "Guide support matrix is unavailable"
            elif engine == "unknown":
                reason = "overlay is not an engine deployment entry point"
            elif not isinstance(support_entry, dict) or engine not in engines:
                reason = "accelerator and engine are not declared in the Guide support matrix"
            elif not (source_root / "guides" / "recipes" / "router" / "base.values.yaml").is_file():
                reason = "router base values are unavailable"
            elif standard_values is None:
                reason = "Guide router values are unavailable"
            entries.append(
                GuideOverlay(
                    guide=guide_root.name,
                    guide_label=guide_label,
                    overlay=str(kustomization.parent.relative_to(source_root)),
                    accelerator=accelerator,
                    accelerator_label=accelerator_label,
                    engine=engine,
                    engine_label=engine_label,
                    variant=variant,
                    variant_label=variant_label,
                    model=str(model) if model else None,
                    maturity=str(maturity) if maturity else None,
                    supported=reason is None,
                    reason=reason,
                )
            )
    return entries


def _guide_descriptor(guide_root: Path) -> dict[str, Any]:
    path = guide_root / "guide.yaml"
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _display_identifier(value: str) -> str:
    words = re.split(r"[-_\s]+", value.strip())
    return " ".join(
        word.upper() if len(word) <= 3 or any(character.isdigit() for character in word) else word.capitalize()
        for word in words
        if word
    )


def _metadata_label(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        label = value.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return " / ".join(_display_identifier(part) for part in fallback.split("/"))


def _overlay_variant_label(support_entry: Any, variant: str, engine: str) -> str:
    providers = support_entry.get("providers") if isinstance(support_entry, Mapping) else None
    candidates: list[tuple[str, str]] = []
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, Mapping):
                continue
            name = provider.get("name")
            label = provider.get("label")
            provider_engines = provider.get("engines")
            if (
                isinstance(name, str)
                and name
                and isinstance(label, str)
                and label
                and (not isinstance(provider_engines, list) or engine in provider_engines)
            ):
                candidates.append((name, label))
    for name, label in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        if variant == name:
            return label
        suffix = f"/{name}"
        if variant.endswith(suffix):
            prefix = variant[: -len(suffix)]
            return f"{_metadata_label(None, prefix)} · {label}"
    return _metadata_label(None, variant)


def _overlay_coordinates(parts: tuple[str, ...], engines: set[str]) -> tuple[str, str, str]:
    engine_index = next((index for index, value in enumerate(parts) if value in engines), None)
    if engine_index is None:
        accelerator = parts[0] if parts else "unknown"
        return accelerator, "unknown", "/".join(parts[1:]) or "."
    accelerator = "/".join(parts[:engine_index]) or "unknown"
    variant = "/".join(parts[engine_index + 1 :]) or "."
    return accelerator, parts[engine_index], variant


def _router_values_path(source_root: Path, guide: str, engine: str) -> Path | None:
    root = source_root / "guides" / guide / "router"
    candidates = [root / f"{guide}-{engine}.values.yaml", root / f"{guide}.values.yaml"]
    return next((path for path in candidates if path.is_file()), None)
