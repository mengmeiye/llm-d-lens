"""Database-free, existing-namespace deployment lifecycle for llm-d Guides."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llm_d_bench.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from llm_d_bench.deploy.guide_steps import (
    GuideManifest,
    GuideSteps,
    GuideStepsError,
    declares_modelserver_steps,
    load_guide_document,
    resolve_guide_steps,
)
from llm_d_bench.hardware.models import HardwareProfile
from llm_d_bench.hardware.resolver import resolve_by_device_class, resolve_by_node_label, resolve_by_resource
from llm_d_bench.utils.kubernetes_names import validate_namespace
from llm_d_bench.utils.paths import prism_temp_root
from llm_d_bench.utils.shell import CommandResult, run
from llm_d_bench.versions import router_chart, router_chart_version

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
DEPLOYMENT_LABEL = "llm-d.ai/deployment"
MANAGED_BY_VALUE = "llmd"
INVENTORY_PREFIX = "llmd-inventory-"
INVENTORY_SCHEMA = "llmd.inventory/v1"
ROUTER_RELEASE_PREFIX = "llmd-"

_STACK_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_MODEL_ID_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_RETAINED_KINDS = {"PersistentVolumeClaim", "Secret"}
_POD_TEMPLATE_KEYS = {"leaderTemplate", "template", "workerTemplate"}
# Router /metrics authentication renders a ClusterRole for TokenReview and
# SubjectAccessReview, which a namespace operator cannot create.
_NAMESPACED_ROUTER_FLAGS = ("--set", "router.monitoring.prometheus.auth.enabled=false")

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


@dataclass(frozen=True)
class DeploymentInventory:
    namespace: str
    name: str
    guide: str
    overlay: str
    model: str | None
    release_name: str
    source_revision: str | None
    resources: tuple[ResourceIdentity, ...]
    retained: tuple[ResourceIdentity, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": INVENTORY_SCHEMA,
            "namespace": self.namespace,
            "name": self.name,
            "guide": self.guide,
            "overlay": self.overlay,
            "model": self.model,
            "releaseName": self.release_name,
            "sourceRevision": self.source_revision,
            "resources": [_identity_payload(item) for item in self.resources],
            "retainedResources": [_identity_payload(item) for item in self.retained],
        }


@dataclass(frozen=True)
class ClusterCapabilities:
    """Non-sensitive, advisory capabilities for one local Kubernetes connection."""

    accelerators: tuple[str, ...]
    accelerator_status: str
    accelerator_reason: str | None
    platform: str | None
    platform_label: str | None
    platform_status: str
    platform_reason: str | None
    recommended_profile: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "accelerators": list(self.accelerators),
            "acceleratorStatus": self.accelerator_status,
            "acceleratorReason": self.accelerator_reason,
            "platform": self.platform,
            "platformLabel": self.platform_label,
            "platformStatus": self.platform_status,
            "platformReason": self.platform_reason,
            "recommendedProfile": self.recommended_profile,
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
        descriptor = load_guide_document(guide_root)
        follows_guide_steps = declares_modelserver_steps(descriptor)
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
            elif follows_guide_steps:
                try:
                    _guide_steps(source_root, descriptor, accelerator, engine, kustomization.parent)
                except DomainValidationError as error:
                    reason = str(error)
            elif not (source_root / "guides" / "recipes" / "router" / "base.values.yaml").is_file():
                reason = "router base values are unavailable"
            elif _router_values_path(source_root, guide_root.name, engine) is None:
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

    async def capabilities(self) -> ClusterCapabilities:
        """Best-effort accelerator and platform hints for the selected context.

        These probes are advisory because namespace operators commonly cannot
        list Nodes or ResourceSlices. A failed or ambiguous probe never changes
        the deployer's namespace-scoped lifecycle contract.
        """

        version, nodes, resource_slices, openshift_resources = await asyncio.gather(
            self._probe(["kubectl", "version", "--output=json"]),
            self._probe(["kubectl", "get", "nodes", "--output=json"]),
            self._probe(
                ["kubectl", "get", "resourceslices.resource.k8s.io", "--output=json"],
            ),
            self._probe(
                ["kubectl", "api-resources", "--api-group=config.openshift.io", "--no-headers"],
            ),
        )
        node_items = _command_items(nodes)
        resource_slice_items = _command_items(resource_slices)
        profiles = _detected_hardware_profiles(node_items, resource_slice_items)
        accelerators = tuple(sorted({profile.upstream_variant for profile in profiles if profile.upstream_variant}))
        accelerator_status, accelerator_reason = _accelerator_detection_status(
            accelerators,
            nodes,
            resource_slices,
            node_items is not None,
            resource_slice_items is not None,
        )
        platform, platform_status, platform_reason = _detected_platform(
            version,
            node_items,
            openshift_resources,
        )
        platform_label, recommended_profile = _platform_presentation(platform)
        return ClusterCapabilities(
            accelerators=accelerators,
            accelerator_status=accelerator_status,
            accelerator_reason=accelerator_reason,
            platform=platform,
            platform_label=platform_label,
            platform_status=platform_status,
            platform_reason=platform_reason,
            recommended_profile=recommended_profile,
        )

    def with_connection(self, kubeconfig: str | Path | None, context: str | None = None) -> NamespaceDeployer:
        """Return an equivalent deployer bound to one Kubernetes connection."""

        return type(self)(
            self.source_root,
            kubeconfig=kubeconfig,
            context=context,
            timeout_seconds=self.timeout_seconds,
            runner=self._runner,
        )

    async def render(
        self,
        *,
        namespace: str,
        name: str,
        guide: str,
        overlay: str,
        model: str | None = None,
        preflight: bool = True,
    ) -> RenderedPlan:
        namespace = validate_namespace(namespace)
        name = _validate_stack_name(name)
        source_root = self._require_source()
        overlay_path, catalog_entry = self._overlay_path(guide, overlay)
        selected_model = _deployment_model(catalog_entry, model)
        steps = self._steps(catalog_entry, overlay_path)

        model_documents: list[dict[str, Any]] = []
        for manifest in steps.manifests:
            if manifest.kustomize:
                result = await self._command(["kubectl", "kustomize", str(manifest.path)])
                model_documents.extend(_parse_documents(result.stdout, "Kustomize"))
            else:
                model_documents.extend(_parse_documents(manifest.path.read_text(encoding="utf-8"), "Guide manifest"))
        if (
            model is not None
            and selected_model != catalog_entry.model
            and not _contains_string(model_documents, catalog_entry.model or "")
        ):
            raise DomainValidationError(
                "the selected Guide overlay does not expose its declared model ID for a safe override"
            )
        _bind_and_label(model_documents, namespace, name)

        release_name = f"{ROUTER_RELEASE_PREFIX}{name}"
        with _model_value_paths(steps.router_values, catalog_entry.model, selected_model) as values_paths:
            router_result = await self._command(
                ["helm", "template", *self._router_arguments(release_name, namespace, name, steps, values_paths)]
            )
        router_documents = _parse_documents(router_result.stdout, "Helm")
        if model is not None:
            _replace_model_references(
                [*model_documents, *router_documents],
                catalog_entry.model,
                selected_model,
            )
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

    async def deploy(
        self,
        *,
        namespace: str,
        name: str,
        guide: str,
        overlay: str,
        model: str | None = None,
    ) -> DeploymentInventory:
        namespace = validate_namespace(namespace)
        name = _validate_stack_name(name)
        await self._refuse_existing_inventory(namespace, name)
        plan = await self.render(
            namespace=namespace,
            name=name,
            guide=guide,
            overlay=overlay,
            model=model,
            preflight=True,
        )
        await self._refuse_existing_resources(namespace, [*plan.resources, *plan.retained])
        await self._refuse_existing_release(namespace, plan.release_name)
        overlay_path, catalog_entry = self._overlay_path(guide, plan.overlay)
        steps = self._steps(catalog_entry, overlay_path)
        helm_installed = False
        model_apply_attempted = False
        try:
            with _model_value_paths(steps.router_values, catalog_entry.model, plan.model) as values_paths:
                await self._command(
                    [
                        "helm",
                        "install",
                        *self._router_arguments(plan.release_name, namespace, name, steps, values_paths),
                        "--atomic",
                        "--timeout",
                        f"{max(1, int(self.timeout_seconds * 0.8))}s",
                    ]
                )
            helm_installed = True
            model_apply_attempted = True
            await self._command(
                ["kubectl", "apply", "--namespace", namespace, "--filename=-"], stdin=plan.model_manifest
            )
            inventory = DeploymentInventory(
                namespace=namespace,
                name=name,
                guide=guide,
                overlay=plan.overlay,
                model=plan.model,
                release_name=plan.release_name,
                source_revision=await self._source_revision(),
                resources=plan.resources,
                retained=plan.retained,
            )
            await self._command(
                ["kubectl", "apply", "--namespace", namespace, "--filename=-"],
                stdin=_inventory_manifest(inventory),
            )
            return inventory
        except Exception:
            if model_apply_attempted:
                owned = await self._owned_resources(namespace, name, plan.resources, check=False)
                await self._delete_resources(namespace, owned, check=False)
            if helm_installed:
                await self._command(["helm", "uninstall", plan.release_name, "--namespace", namespace], check=False)
            raise

    async def list(self, namespace: str) -> list[dict[str, Any]]:  # noqa: A003 - domain operation name
        namespace = validate_namespace(namespace)
        result = await self._command(
            [
                "kubectl",
                "get",
                "configmaps",
                "--namespace",
                namespace,
                "--selector",
                f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
                "--output=json",
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DeploymentCommandError("Kubernetes returned an invalid ConfigMap list") from error
        inventories = []
        for item in payload.get("items", []):
            try:
                inventory = _inventory_from_configmap(item)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            inventories.append({**inventory.payload(), "status": "installed"})
        return sorted(inventories, key=lambda item: item["name"])

    async def status(self, namespace: str, name: str) -> dict[str, Any]:
        inventory = await self._load_inventory(namespace, name)
        resources = []
        missing = 0
        for identity in inventory.resources:
            result = await self._command(
                [
                    "kubectl",
                    "get",
                    _kubectl_identity(identity),
                    "--namespace",
                    inventory.namespace,
                    "--output=name",
                ],
                check=False,
            )
            present = result.returncode == 0
            if not present and "notfound" not in (result.stderr + result.stdout).replace(" ", "").lower():
                raise DeploymentCommandError(
                    _command_detail(result, f"Unable to inspect {identity.kind}/{identity.name}")
                )
            missing += 0 if present else 1
            resources.append({**_identity_payload(identity), "present": present})
        release_present, release_status = await self._release_status(inventory.namespace, inventory.release_name)
        return {
            **inventory.payload(),
            "status": "installed" if missing == 0 and release_status == "deployed" else "degraded",
            "resources": resources,
            "missingResources": missing,
            "releasePresent": release_present,
            "releaseStatus": release_status,
        }

    async def teardown(self, namespace: str, name: str) -> dict[str, Any]:
        inventory = await self._load_inventory(namespace, name)
        await self._validate_namespaced_identities(inventory.resources)
        owned = await self._owned_resources(inventory.namespace, inventory.name, inventory.resources, check=True)
        helm = await self._command(
            ["helm", "uninstall", inventory.release_name, "--namespace", inventory.namespace], check=False
        )
        if helm.returncode and "not found" not in (helm.stderr + helm.stdout).lower():
            raise DeploymentCommandError(_command_detail(helm, "Helm uninstall failed"))
        await self._delete_resources(inventory.namespace, owned, check=True)
        await self._command(
            [
                "kubectl",
                "delete",
                "configmap",
                _inventory_name(inventory.name),
                "--namespace",
                inventory.namespace,
                "--ignore-not-found=true",
            ]
        )
        return {
            "namespace": inventory.namespace,
            "name": inventory.name,
            "status": "removed",
            "retainedResources": [_identity_payload(item) for item in inventory.retained],
        }

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

    def _steps(self, entry: GuideOverlay, overlay_path: Path) -> GuideSteps:
        source_root = self._require_source()
        document = load_guide_document(source_root / "guides" / entry.guide)
        return _guide_steps(source_root, document, entry.accelerator, entry.engine, overlay_path)

    def _router_arguments(
        self,
        release_name: str,
        namespace: str,
        name: str,
        steps: GuideSteps,
        values_paths: Sequence[Path],
    ) -> list[str]:
        arguments = [
            release_name,
            steps.router_chart,
            "--namespace",
            namespace,
            "--version",
            steps.router_chart_version,
        ]
        for path in values_paths:
            arguments.extend(["--values", str(path)])
        return [*arguments, *_NAMESPACED_ROUTER_FLAGS, "--set-string", _router_stack_selector(name)]

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

    async def _validate_namespaced_identities(self, resources: Sequence[ResourceIdentity]) -> None:
        scope = await self._api_resource_scopes()
        for identity in resources:
            key = (identity.api_version, identity.kind)
            if key not in scope:
                raise DomainValidationError(
                    f"cluster no longer serves inventoried {identity.api_version} {identity.kind}; refusing teardown"
                )
            if not scope[key]:
                raise DomainValidationError(
                    f"inventory contains cluster-scoped {identity.api_version} "
                    f"{identity.kind}/{identity.name}; refusing teardown"
                )

    async def _api_resource_scopes(self) -> dict[tuple[str, str], bool]:
        discovery = await self._command(["kubectl", "api-resources", "--output=wide"])
        return _parse_api_resource_scopes(discovery.stdout)

    async def _server_dry_run(self, namespace: str, model_manifest: str, router_manifest: str) -> None:
        documents = [
            *_parse_documents(router_manifest, "Helm"),
            *(yaml.safe_load_all(model_manifest) if model_manifest.strip() else ()),
        ]
        # A server dry-run stores nothing, so a RoleBinding to a Role from the same plan
        # fails its escalation check for users without the RBAC "bind" verb. The Role
        # itself is dry-run (proving the user holds its rules), and Helm or kubectl
        # create it before the binding, so only the right to create bindings is checked.
        planned_roles = {
            document["metadata"]["name"]
            for document in documents
            if isinstance(document, dict) and document.get("kind") == "Role"
        }
        deferred = [
            document
            for document in documents
            if isinstance(document, dict)
            and document.get("kind") == "RoleBinding"
            and (document.get("roleRef") or {}).get("kind") == "Role"
            and (document.get("roleRef") or {}).get("name") in planned_roles
        ]
        checked = [document for document in documents if isinstance(document, dict) and document not in deferred]
        if checked:
            await self._command(
                ["kubectl", "apply", "--dry-run=server", "--namespace", namespace, "--filename=-"],
                stdin=yaml.safe_dump_all(checked, sort_keys=False),
            )
        if deferred:
            result = await self._command(
                [
                    "kubectl",
                    "auth",
                    "can-i",
                    "create",
                    "rolebindings.rbac.authorization.k8s.io",
                    "--namespace",
                    namespace,
                ],
                check=False,
            )
            if result.stdout.strip() != "yes":
                raise DomainValidationError(f"the plan creates RoleBindings, which are not allowed in {namespace!r}")

    async def _refuse_existing_inventory(self, namespace: str, name: str) -> None:
        result = await self._command(
            ["kubectl", "get", "configmap", _inventory_name(name), "--namespace", namespace, "--output=json"],
            check=False,
        )
        if result.returncode == 0:
            raise ConflictError(f"deployment {name!r} already has an inventory in namespace {namespace!r}")
        if "notfound" not in (result.stderr + result.stdout).replace(" ", "").lower():
            raise DeploymentCommandError(_command_detail(result, "Unable to check the deployment inventory"))

    async def _load_inventory(self, namespace: str, name: str) -> DeploymentInventory:
        namespace = validate_namespace(namespace)
        name = _validate_stack_name(name)
        result = await self._command(
            ["kubectl", "get", "configmap", _inventory_name(name), "--namespace", namespace, "--output=json"],
            check=False,
        )
        if result.returncode:
            if "notfound" in (result.stderr + result.stdout).replace(" ", "").lower():
                raise NotFoundError(f"deployment inventory {name!r} was not found in namespace {namespace!r}")
            raise DeploymentCommandError(_command_detail(result, "Unable to read the deployment inventory"))
        try:
            inventory = _inventory_from_configmap(json.loads(result.stdout))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DomainValidationError(f"deployment inventory {name!r} is malformed; refusing teardown") from error
        if inventory.namespace != namespace or inventory.name != name:
            raise DomainValidationError("deployment inventory identity does not match the requested deployment")
        return inventory

    async def _refuse_existing_resources(self, namespace: str, resources: Sequence[ResourceIdentity]) -> None:
        for identity in resources:
            result = await self._command(
                ["kubectl", "get", _kubectl_identity(identity), "--namespace", namespace, "--output=name"],
                check=False,
            )
            if result.returncode == 0:
                raise ConflictError(
                    f"rendered {identity.kind}/{identity.name} already exists in namespace {namespace!r}"
                )
            if "notfound" not in (result.stderr + result.stdout).replace(" ", "").lower():
                raise DeploymentCommandError(
                    _command_detail(result, f"Unable to check {identity.kind}/{identity.name}")
                )

    async def _refuse_existing_release(self, namespace: str, release_name: str) -> None:
        result = await self._command(
            ["helm", "status", release_name, "--namespace", namespace, "--output=json"],
            check=False,
        )
        if result.returncode == 0:
            raise ConflictError(f"Helm release {release_name!r} already exists in namespace {namespace!r}")
        detail = (result.stderr + result.stdout).lower()
        if "release: not found" not in detail and "release not found" not in detail:
            raise DeploymentCommandError(_command_detail(result, "Unable to check the Helm release"))

    async def _release_status(self, namespace: str, release_name: str) -> tuple[bool, str | None]:
        result = await self._command(
            ["helm", "status", release_name, "--namespace", namespace, "--output=json"],
            check=False,
        )
        if result.returncode:
            detail = (result.stderr + result.stdout).lower()
            if "release: not found" in detail or "release not found" in detail:
                return False, None
            raise DeploymentCommandError(_command_detail(result, "Unable to inspect the Helm release"))
        try:
            payload = json.loads(result.stdout)
            info = payload.get("info") if isinstance(payload, dict) else None
            status = info.get("status") if isinstance(info, dict) else None
        except json.JSONDecodeError as error:
            raise DeploymentCommandError("Helm returned invalid release status") from error
        if not isinstance(status, str) or not status:
            raise DeploymentCommandError("Helm returned incomplete release status")
        return True, status

    async def _delete_resources(self, namespace: str, resources: Sequence[ResourceIdentity], *, check: bool) -> None:
        for identity in reversed(resources):
            await self._command(
                [
                    "kubectl",
                    "delete",
                    _kubectl_identity(identity),
                    "--namespace",
                    namespace,
                    "--ignore-not-found=true",
                ],
                check=check,
            )

    async def _owned_resources(
        self,
        namespace: str,
        name: str,
        resources: Sequence[ResourceIdentity],
        *,
        check: bool,
    ) -> list[ResourceIdentity]:
        owned = []
        for identity in resources:
            result = await self._command(
                ["kubectl", "get", _kubectl_identity(identity), "--namespace", namespace, "--output=json"],
                check=False,
            )
            if result.returncode:
                if "notfound" in (result.stderr + result.stdout).replace(" ", "").lower():
                    continue
                if check:
                    raise DeploymentCommandError(
                        _command_detail(result, f"Unable to verify {identity.kind}/{identity.name} ownership")
                    )
                continue
            try:
                document = json.loads(result.stdout)
                metadata = document.get("metadata") if isinstance(document, dict) else None
                labels = metadata.get("labels") if isinstance(metadata, dict) else None
            except json.JSONDecodeError as error:
                if check:
                    raise DeploymentCommandError(
                        f"Kubernetes returned invalid ownership data for {identity.kind}/{identity.name}"
                    ) from error
                continue
            expected = {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                DEPLOYMENT_LABEL: name,
            }
            if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected.items()):
                if check:
                    raise ConflictError(
                        f"{identity.kind}/{identity.name} no longer carries deployment {name!r} ownership labels"
                    )
                continue
            owned.append(identity)
        return owned

    async def _source_revision(self) -> str | None:
        source_root = self._require_source()
        result = await self._command(["git", "-C", str(source_root), "rev-parse", "HEAD"], check=False)
        revision = result.stdout.strip()
        return revision if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision) else None

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

    async def _probe(self, argv: list[str]) -> CommandResult:
        """Run one optional read probe without making detection a prerequisite."""

        try:
            return await self._command(argv, check=False, timeout_seconds=10)
        except (OSError, TimeoutError):
            return CommandResult(tuple(self._connection_arguments(argv)), 1, "", "probe unavailable")

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


def _guide_steps(
    source_root: Path,
    document: Mapping[str, Any],
    accelerator: str,
    engine: str,
    overlay_path: Path,
) -> GuideSteps:
    """The Guide's own router and manifest steps, or the legacy layout convention when it declares none."""

    if declares_modelserver_steps(document):
        try:
            return resolve_guide_steps(
                source_root,
                document,
                accelerator=accelerator,
                engine=engine,
                overlay=overlay_path,
                defaults={
                    "ROUTER_STANDALONE_CHART": router_chart(),
                    "ROUTER_CHART_VERSION": router_chart_version(),
                },
            )
        except GuideStepsError as error:
            raise DomainValidationError(str(error)) from error
    guide = overlay_path.relative_to(source_root / "guides").parts[0]
    values_path = _router_values_path(source_root, guide, engine)
    base_values = source_root / "guides" / "recipes" / "router" / "base.values.yaml"
    if values_path is None or not base_values.is_file():
        raise DomainValidationError(f"Guide {guide!r} has no supported standalone router values")
    return GuideSteps(
        router_chart=router_chart(),
        router_chart_version=router_chart_version(),
        router_values=(base_values, values_path),
        manifests=(GuideManifest(kustomize=True, path=overlay_path),),
    )


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


def _command_items(result: CommandResult) -> list[dict[str, Any]] | None:
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return None
    return items


def _detected_hardware_profiles(
    nodes: list[dict[str, Any]] | None,
    resource_slices: list[dict[str, Any]] | None,
) -> tuple[HardwareProfile, ...]:
    profiles: dict[str, HardwareProfile] = {}
    for node in nodes or []:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        profile = resolve_by_node_label({str(key): str(value) for key, value in labels.items()})
        if profile:
            profiles[profile.id] = profile
        status = node.get("status") if isinstance(node.get("status"), dict) else {}
        for field_name in ("capacity", "allocatable"):
            resources = status.get(field_name) if isinstance(status.get(field_name), dict) else {}
            for resource_name in resources:
                profile = resolve_by_resource(str(resource_name))
                if profile:
                    profiles[profile.id] = profile
    for resource_slice in resource_slices or []:
        spec = resource_slice.get("spec") if isinstance(resource_slice.get("spec"), dict) else {}
        profile = resolve_by_device_class(str(spec.get("driver") or ""))
        if profile:
            profiles[profile.id] = profile
    return tuple(profiles.values())


def _permission_denied(result: CommandResult) -> bool:
    detail = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in detail for marker in ("forbidden", "unauthorized", "permission denied"))


def _accelerator_detection_status(
    accelerators: tuple[str, ...],
    nodes: CommandResult,
    resource_slices: CommandResult,
    nodes_readable: bool,
    resource_slices_readable: bool,
) -> tuple[str, str | None]:
    if len(accelerators) == 1:
        return "detected", None
    if len(accelerators) > 1:
        return "multiple", "Multiple registered accelerator types were detected; choose one manually."
    if nodes_readable or resource_slices_readable:
        return "not-detected", "No registered accelerator type was detected; choose one manually."
    if _permission_denied(nodes) or _permission_denied(resource_slices):
        return (
            "permission-limited",
            "Accelerator detection needs read access to Nodes or ResourceSlices; choose one manually.",
        )
    return "unavailable", "Accelerator detection is unavailable; choose one manually."


def _detected_platform(
    version: CommandResult,
    nodes: list[dict[str, Any]] | None,
    openshift_resources: CommandResult,
) -> tuple[str | None, str, str | None]:
    detected: set[str] = set()
    if openshift_resources.returncode == 0 and openshift_resources.stdout.strip():
        detected.add("openshift")
    for node in nodes or []:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        label_names = {str(key) for key in labels}
        spec = node.get("spec") if isinstance(node.get("spec"), dict) else {}
        provider_id = str(spec.get("providerID") or "").lower()
        if any(name.startswith("cloud.google.com/gke-") for name in label_names):
            detected.add("gke")
        if any(name.startswith("eks.amazonaws.com/") for name in label_names) or provider_id.startswith("aws://"):
            detected.add("aws")
        if any(name.startswith(("machine.openshift.io/", "node.openshift.io/")) for name in label_names):
            detected.add("openshift")
    if version.returncode == 0:
        try:
            payload = json.loads(version.stdout)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        server = payload.get("serverVersion") if isinstance(payload, dict) else {}
        git_version = str(server.get("gitVersion") or "").lower() if isinstance(server, dict) else ""
        if "gke" in git_version:
            detected.add("gke")
        if "eks" in git_version:
            detected.add("aws")
    if len(detected) == 1:
        return next(iter(detected)), "detected", None
    if len(detected) > 1:
        return None, "multiple", "Multiple cluster platform signals were detected; choose a profile manually."
    if version.returncode == 0:
        return "kubernetes", "detected", None
    if _permission_denied(openshift_resources):
        return None, "permission-limited", "Platform detection is permission-limited; choose a profile manually."
    return None, "unavailable", "Platform detection is unavailable; choose a profile manually."


def _platform_presentation(platform: str | None) -> tuple[str | None, str | None]:
    values = {
        "aws": ("Amazon EKS", "aws"),
        "gke": ("Google Kubernetes Engine", "gke"),
        "kubernetes": ("Kubernetes", "base"),
        "openshift": ("Red Hat OpenShift", "base"),
    }
    return values.get(platform, (None, None))


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


def _deployment_model(entry: GuideOverlay, value: str | None) -> str | None:
    if value is None:
        return entry.model
    model = value.strip()
    parts = model.split("/")
    if (
        len(model) > 96
        or len(parts) != 2
        or any(not part or not _MODEL_ID_PART.fullmatch(part) for part in parts)
        or "--" in model
        or ".." in model
    ):
        raise DomainValidationError("model must be a Hugging Face model ID such as organization/model-name")
    if not entry.model:
        raise DomainValidationError("the selected Guide overlay does not declare a model that Lens can override")
    return model


def _inventory_name(name: str) -> str:
    return f"{INVENTORY_PREFIX}{name}"


def _parse_documents(content: str, source: str) -> list[dict[str, Any]]:
    try:
        documents = [item for item in yaml.safe_load_all(content) if item is not None]
    except yaml.YAMLError as error:
        raise DomainValidationError(f"{source} returned invalid YAML") from error
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise DomainValidationError(f"{source} did not render Kubernetes objects")
    return documents


def _replace_model_references(
    documents: Sequence[dict[str, Any]],
    default_model: str | None,
    selected_model: str | None,
) -> None:
    if not default_model or not selected_model or default_model == selected_model:
        return
    default_name = default_model.rsplit("/", 1)[-1]
    selected_name = selected_model.rsplit("/", 1)[-1]
    replacements = ((default_model, selected_model), (default_name, selected_name))

    for document in documents:
        _replace_model_value(document, replacements, selected_name, selected_model)


def _contains_string(value: Any, needle: str) -> bool:
    if not needle:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return any(_contains_string(child, needle) for child in value)
    if isinstance(value, dict):
        return any(_contains_string(child, needle) for child in value.values())
    return False


def _replace_model_value(
    value: Any,
    replacements: Sequence[tuple[str, str]],
    selected_name: str,
    selected_model: str,
) -> Any:
    if isinstance(value, str):
        by_source = dict(replacements)
        pattern = "|".join(re.escape(source) for source, _replacement in replacements)
        return re.sub(pattern, lambda match: by_source[match.group(0)], value)
    if isinstance(value, list):
        for index, child in enumerate(value):
            value[index] = _replace_model_value(child, replacements, selected_name, selected_model)
        return value
    if isinstance(value, dict):
        for key, child in value.items():
            value[key] = _replace_model_value(child, replacements, selected_name, selected_model)
        if "llm-d.ai/model" in value:
            value["llm-d.ai/model"] = _model_label(selected_name, selected_model)
    return value


@contextmanager
def _model_value_paths(
    paths: Sequence[Path],
    default_model: str | None,
    selected_model: str | None,
) -> Iterator[tuple[Path, ...]]:
    if not default_model or not selected_model or default_model == selected_model:
        yield tuple(paths)
        return
    default_name = default_model.rsplit("/", 1)[-1]
    selected_name = selected_model.rsplit("/", 1)[-1]
    replacements = ((default_model, selected_model), (default_name, selected_name))
    scratch = prism_temp_root("llmd-model-values")
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="values-", dir=scratch) as temporary:
        rewritten = []
        for index, path in enumerate(paths):
            try:
                values = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as error:
                raise DomainValidationError(f"router values {path.name!r} could not be read") from error
            if not isinstance(values, dict):
                raise DomainValidationError(f"router values {path.name!r} must contain a mapping")
            _replace_model_value(values, replacements, selected_name, selected_model)
            destination = Path(temporary) / f"{index}-{path.name}"
            destination.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
            destination.chmod(0o600)
            rewritten.append(destination)
        yield tuple(rewritten)


def _model_label(model_name: str, model_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", model_name).strip("._-")
    if not value:
        value = "model"
    if len(value) <= 63:
        return value
    digest = hashlib.sha256(model_id.encode()).hexdigest()[:8]
    return f"{value[:54].rstrip('._-')}-{digest}"


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
        if key in _POD_TEMPLATE_KEYS and isinstance(child, dict) and isinstance(child.get("spec"), dict):
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


def _identity_from_payload(payload: Mapping[str, Any]) -> ResourceIdentity:
    identity = ResourceIdentity(
        api_version=str(payload["apiVersion"]), kind=str(payload["kind"]), name=str(payload["name"])
    )
    if not all(asdict(identity).values()):
        raise ValueError("resource identity is incomplete")
    return identity


def _kubectl_identity(identity: ResourceIdentity) -> str:
    _, separator, _ = identity.api_version.partition("/")
    group = identity.api_version.split("/", 1)[0] if separator else ""
    kind = identity.kind.lower()
    resource = f"{kind}.{group}" if group else kind
    return f"{resource}/{identity.name}"


def _inventory_manifest(inventory: DeploymentInventory) -> str:
    payload = json.dumps(inventory.payload(), sort_keys=True, separators=(",", ":"))
    document = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _inventory_name(inventory.name),
            "namespace": inventory.namespace,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE, DEPLOYMENT_LABEL: inventory.name},
        },
        "data": {"inventory.json": payload},
    }
    return yaml.safe_dump(document, sort_keys=False)


def _inventory_from_configmap(document: Mapping[str, Any]) -> DeploymentInventory:
    metadata = document["metadata"]
    raw = document["data"]["inventory.json"]
    payload = json.loads(raw)
    if payload.get("schemaVersion") != INVENTORY_SCHEMA:
        raise ValueError("unsupported inventory schema")
    inventory = DeploymentInventory(
        namespace=str(payload["namespace"]),
        name=str(payload["name"]),
        guide=str(payload["guide"]),
        overlay=str(payload["overlay"]),
        model=str(payload["model"]) if payload.get("model") else None,
        release_name=str(payload["releaseName"]),
        source_revision=str(payload["sourceRevision"]) if payload.get("sourceRevision") else None,
        resources=tuple(_identity_from_payload(item) for item in payload["resources"]),
        retained=tuple(_identity_from_payload(item) for item in payload.get("retainedResources", [])),
    )
    if metadata.get("name") != _inventory_name(inventory.name) or metadata.get("namespace") not in (
        None,
        inventory.namespace,
    ):
        raise ValueError("inventory ConfigMap identity does not match its payload")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    expected_labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, DEPLOYMENT_LABEL: inventory.name}
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected_labels.items()):
        raise ValueError("inventory ConfigMap does not carry its stack ownership labels")
    if inventory.release_name != f"{ROUTER_RELEASE_PREFIX}{inventory.name}":
        raise ValueError("inventory Helm release does not match its stack name")
    return inventory


def _command_detail(result: CommandResult, fallback: str) -> str:
    detail = (result.stderr or result.stdout).strip()
    return detail or fallback
