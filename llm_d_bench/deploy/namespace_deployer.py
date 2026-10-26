"""Database-free, existing-namespace deployment lifecycle for llm-d Guides."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.core.exceptions import DomainValidationError
from llm_d_bench.utils.kubernetes_names import validate_namespace
from llm_d_bench.utils.shell import CommandResult, run
from llm_d_bench.versions import router_chart, router_chart_version

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
DEPLOYMENT_LABEL = "llm-d.ai/deployment"
MANAGED_BY_VALUE = "llmd"
ROUTER_RELEASE_PREFIX = "llmd-"
_STACK_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_RETAINED_KINDS = {"PersistentVolumeClaim", "Secret"}
CommandRunner = Callable[..., Awaitable[CommandResult]]


class DeploymentCommandError(RuntimeError):
    """A required Kubernetes, Kustomize, or Helm command failed."""


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


@dataclass(frozen=True)
class ResourceIdentity:
    api_version: str
    kind: str
    name: str


@dataclass(frozen=True)
class RenderedPlan:
    namespace: str
    name: str
    guide: str
    overlay: str
    model: str | None
    release_name: str
    resources: tuple[ResourceIdentity, ...]
    retained: tuple[ResourceIdentity, ...]
    model_manifest: str = field(repr=False)
    router_manifest: str = field(repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "guide": self.guide,
            "overlay": self.overlay,
            "model": self.model,
            "releaseName": self.release_name,
            "resources": [_identity_payload(item) for item in self.resources],
            "retainedResources": [_identity_payload(item) for item in self.retained],
        }


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


class NamespaceDeployer:
    """Render and own a stack without creating or deleting its namespace."""

    def __init__(
        self,
        source_root: str | Path | None = None,
        *,
        kubeconfig: str | Path | None = None,
        context: str | None = None,
        timeout_seconds: float = 300,
        runner: CommandRunner = run,
    ) -> None:
        if source_root is not None or os.environ.get("LLM_D_ROOT"):
            self.source_root: Path | None = resolve_source_root(source_root)
        else:
            try:
                self.source_root = resolve_source_root()
            except DomainValidationError:
                self.source_root = None
        self.kubeconfig = str(Path(kubeconfig).expanduser()) if kubeconfig else None
        self.context = context.strip() if context else None
        self.timeout_seconds = timeout_seconds
        self._runner = runner

    def catalog(self) -> list[GuideOverlay]:
        return discover_guide_overlays(self._require_source())

    async def render(
        self,
        *,
        namespace: str,
        name: str,
        guide: str,
        overlay: str,
        preflight: bool = True,
    ) -> RenderedPlan:
        namespace = validate_namespace(namespace)
        name = _validate_stack_name(name)
        source_root = self._require_source()
        overlay_path, catalog_entry = self._overlay_path(guide, overlay)
        selected_model = catalog_entry.model
        values_path = _router_values_path(source_root, guide, catalog_entry.engine)
        if values_path is None:
            raise DomainValidationError(f"Guide {guide!r} has no supported standalone router values")
        base_values = source_root / "guides" / "recipes" / "router" / "base.values.yaml"
        if not base_values.is_file():
            raise DomainValidationError("source is missing guides/recipes/router/base.values.yaml")

        model_result = await self._command(["kubectl", "kustomize", str(overlay_path)])
        model_documents = _parse_documents(model_result.stdout, "Kustomize")
        _bind_and_label(model_documents, namespace, name)

        release_name = f"{ROUTER_RELEASE_PREFIX}{name}"
        helm_args = [
            "template",
            release_name,
            router_chart(),
            "--namespace",
            namespace,
            "--version",
            router_chart_version(),
            "--values",
            str(base_values),
            "--values",
            str(values_path),
            "--set-string",
            _router_stack_selector(name),
        ]
        router_result = await self._command(["helm", *helm_args])
        router_documents = _parse_documents(router_result.stdout, "Helm")
        await self._validate_namespaced_documents([*model_documents, *router_documents], namespace)

        model_manifest = yaml.safe_dump_all(model_documents, sort_keys=False)
        router_manifest = yaml.safe_dump_all(router_documents, sort_keys=False)
        resources, retained = _resource_inventory(model_documents)
        plan = RenderedPlan(
            namespace=namespace,
            name=name,
            guide=guide,
            overlay=str(overlay_path.relative_to(source_root)),
            model=selected_model,
            release_name=release_name,
            resources=resources,
            retained=retained,
            model_manifest=model_manifest,
            router_manifest=router_manifest,
        )
        if preflight:
            await self._server_dry_run(namespace, model_manifest, router_manifest)
        return plan

    def _overlay_path(self, guide: str, overlay: str) -> tuple[Path, GuideOverlay]:
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", guide):
            raise DomainValidationError("guide must be a valid Guide directory name")
        source_root = self._require_source()
        path = (source_root / overlay).resolve()
        required_root = (source_root / "guides" / guide / "modelserver").resolve()
        if not path.is_relative_to(required_root):
            raise DomainValidationError("overlay must be a discovered modelserver Kustomize directory")
        if not (path / "kustomization.yaml").is_file() and not (path / "kustomization.yml").is_file():
            raise DomainValidationError("overlay must be a discovered modelserver Kustomize directory")
        relative = str(path.relative_to(source_root))
        catalog_entry = next((item for item in self.catalog() if item.overlay == relative), None)
        if catalog_entry is None:
            raise DomainValidationError("overlay is not present in the Guide catalog")
        if not catalog_entry.supported:
            raise DomainValidationError(catalog_entry.reason or "overlay is not supported by this deployer")
        return path, catalog_entry

    async def _validate_namespaced_documents(self, documents: Sequence[dict[str, Any]], namespace: str) -> None:
        scope = await self._api_resource_scopes()
        for document in documents:
            api_version = str(document.get("apiVersion") or "")
            kind = str(document.get("kind") or "")
            metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
            name = str(metadata.get("name") or "")
            if not api_version or not kind or not name:
                raise DomainValidationError("every rendered object must have apiVersion, kind, and metadata.name")
            key = (api_version, kind)
            if key not in scope:
                raise DomainValidationError(
                    f"cluster does not serve {api_version} {kind}; install its prerequisite first"
                )
            if not scope[key]:
                raise DomainValidationError(f"rendered plan contains cluster-scoped {api_version} {kind}/{name}")
            explicit_namespace = metadata.get("namespace")
            if explicit_namespace not in (None, "", namespace):
                raise DomainValidationError(
                    f"rendered {kind}/{name} targets namespace {explicit_namespace!r}, not {namespace!r}"
                )

    async def _api_resource_scopes(self) -> dict[tuple[str, str], bool]:
        discovery = await self._command(["kubectl", "api-resources", "--output=wide"])
        return _parse_api_resource_scopes(discovery.stdout)

    async def _server_dry_run(self, namespace: str, model_manifest: str, router_manifest: str) -> None:
        await self._command(
            ["kubectl", "apply", "--dry-run=server", "--namespace", namespace, "--filename=-"],
            stdin=f"{router_manifest}\n---\n{model_manifest}",
        )

    def _require_source(self) -> Path:
        if self.source_root is None:
            raise DomainValidationError("this operation requires --source or LLM_D_ROOT")
        return self.source_root

    async def _command(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        command = self._connection_arguments(argv)
        result = await self._runner(command, input=stdin, timeout=timeout_seconds or self.timeout_seconds)
        if check and result.returncode:
            raise DeploymentCommandError(_command_detail(result, f"{Path(argv[0]).name} command failed"))
        return result

    def _connection_arguments(self, argv: list[str]) -> list[str]:
        if not argv:
            return argv
        if argv[0] == "kubectl":
            connection = []
            if self.kubeconfig:
                connection.extend(["--kubeconfig", self.kubeconfig])
            if self.context:
                connection.extend(["--context", self.context])
            return ["kubectl", *connection, *argv[1:]]
        if argv[0] == "helm":
            connection = []
            if self.kubeconfig:
                connection.extend(["--kubeconfig", self.kubeconfig])
            if self.context:
                connection.extend(["--kube-context", self.context])
            return ["helm", *connection, *argv[1:]]
        return argv


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


def _validate_stack_name(value: str) -> str:
    if len(value) > 47 or not _STACK_NAME.fullmatch(value):
        raise DomainValidationError("name must be a Kubernetes DNS label of at most 47 characters")
    return value


def _parse_documents(content: str, source: str) -> list[dict[str, Any]]:
    try:
        documents = [item for item in yaml.safe_load_all(content) if item is not None]
    except yaml.YAMLError as error:
        raise DomainValidationError(f"{source} returned invalid YAML") from error
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise DomainValidationError(f"{source} did not render Kubernetes objects")
    return documents


def _bind_and_label(documents: Sequence[dict[str, Any]], namespace: str, name: str) -> None:
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, DEPLOYMENT_LABEL: name}
    for document in documents:
        metadata = document.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise DomainValidationError("rendered object metadata must be a mapping")
        explicit_namespace = metadata.get("namespace")
        if explicit_namespace not in (None, "", namespace):
            kind = document.get("kind") or "object"
            object_name = metadata.get("name") or "unnamed"
            raise DomainValidationError(
                f"rendered {kind}/{object_name} targets namespace {explicit_namespace!r}, not {namespace!r}"
            )
        existing_labels = metadata.get("labels") or {}
        if not isinstance(existing_labels, dict):
            raise DomainValidationError("rendered object metadata labels must be a mapping")
        metadata["namespace"] = namespace
        metadata["labels"] = {**existing_labels, **labels}
        _label_embedded_templates(document.get("spec"), labels)


def _label_embedded_templates(value: Any, labels: Mapping[str, str]) -> None:
    if isinstance(value, list):
        for item in value:
            _label_embedded_templates(item, labels)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if (
            key in {"leaderWorkerTemplate", "template", "workerTemplate"}
            and isinstance(child, dict)
            and isinstance(child.get("spec"), dict)
        ):
            metadata = child.setdefault("metadata", {})
            if not isinstance(metadata, dict) or not isinstance(metadata.get("labels", {}), dict):
                raise DomainValidationError("rendered workload template metadata labels must be a mapping")
            metadata["labels"] = {**(metadata.get("labels") or {}), **labels}
        _label_embedded_templates(child, labels)


def _router_stack_selector(name: str) -> str:
    return f"router.modelServers.matchLabels.llm-d\\.ai/deployment={name}"


def _parse_api_resource_scopes(output: str) -> dict[tuple[str, str], bool]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise DeploymentCommandError("Kubernetes API discovery returned no resources")
    headers = ("NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND", "VERBS")
    starts = [lines[0].find(header) for header in headers]
    if any(start < 0 for start in starts) or starts != sorted(starts):
        raise DeploymentCommandError("Kubernetes API discovery returned an unsupported table")

    scopes: dict[tuple[str, str], bool] = {}
    for line in lines[1:]:
        columns = [line[starts[index] : starts[index + 1]].strip() for index in range(len(starts) - 1)]
        columns.append(line[starts[-1] :].strip())
        if columns[3] not in {"true", "false"}:
            continue
        scopes[(columns[2], columns[4])] = columns[3] == "true"
    if not scopes:
        raise DeploymentCommandError("Kubernetes API discovery returned no resources")
    return scopes


def _resource_inventory(
    documents: Sequence[dict[str, Any]],
) -> tuple[tuple[ResourceIdentity, ...], tuple[ResourceIdentity, ...]]:
    resources = []
    retained = []
    for document in documents:
        metadata = document.get("metadata") or {}
        identity = ResourceIdentity(
            api_version=str(document.get("apiVersion") or ""),
            kind=str(document.get("kind") or ""),
            name=str(metadata.get("name") or ""),
        )
        (retained if identity.kind in _RETAINED_KINDS else resources).append(identity)
    return tuple(resources), tuple(retained)


def _identity_payload(identity: ResourceIdentity) -> dict[str, str]:
    return {"apiVersion": identity.api_version, "kind": identity.kind, "name": identity.name}


def _command_detail(result: CommandResult, fallback: str) -> str:
    detail = (result.stderr or result.stdout).strip()
    return detail or fallback
