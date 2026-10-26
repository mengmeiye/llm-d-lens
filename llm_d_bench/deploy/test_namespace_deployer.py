from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from llm_d_bench.core.exceptions import ConflictError, DomainValidationError
from llm_d_bench.deploy.cli import main as cli_main
from llm_d_bench.deploy.local_api import create_local_app
from llm_d_bench.deploy.namespace_deployer import (
    DEPLOYMENT_LABEL,
    DeploymentCommandError,
    NamespaceDeployer,
    discover_guide_overlays,
)
from llm_d_bench.utils.shell import CommandResult

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


def _result(argv, returncode=0, stdout="", stderr=""):
    return CommandResult(tuple(str(item) for item in argv), returncode, stdout, stderr)


_DISCOVERY = (
    ("deployments", "deploy", "apps/v1", "true", "Deployment"),
    ("persistentvolumeclaims", "pvc", "v1", "true", "PersistentVolumeClaim"),
    ("services", "svc", "v1", "true", "Service"),
    ("namespaces", "ns", "v1", "false", "Namespace"),
    ("configmaps", "cm", "v1", "true", "ConfigMap"),
)


def _discovery_table(rows) -> str:
    """Render ``kubectl api-resources --output=wide`` with aligned columns."""
    table = [("NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND", "VERBS")]
    table.extend((*row, "[create delete get]") for row in rows)
    widths = [max(len(line[index]) for line in table) + 2 for index in range(5)]
    return "".join(
        "".join(cell.ljust(width) for cell, width in zip(line[:5], widths, strict=True)) + line[5] + "\n"
        for line in table
    )


class FakeRunner:
    def __init__(self, *, cluster_scoped_router: bool = False):
        self.commands = []
        self.inputs = []
        self.inventory = None
        self.resources = {}
        self.release_installed = False
        self.cluster_scoped_router = cluster_scoped_router
        self.helm_values = []

    async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002 - matches shell contract
        self.commands.append(list(argv))
        self.inputs.append(input)
        command = list(argv)
        if command[:2] == ["kubectl", "kustomize"]:
            return _result(
                argv,
                stdout=yaml.safe_dump_all(
                    [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "model-server", "labels": {"llm-d.ai/model": "model"}},
                            "spec": {
                                "template": {
                                    "metadata": {"labels": {"llm-d.ai/model": "model"}},
                                    "spec": {"containers": [{"name": "modelserver", "args": ["example/model"]}]},
                                }
                            },
                        },
                        {
                            "apiVersion": "v1",
                            "kind": "PersistentVolumeClaim",
                            "metadata": {"name": "model-weights"},
                        },
                    ],
                    sort_keys=False,
                ),
            )
        if command[:2] == ["helm", "template"]:
            self.helm_values.extend(
                Path(command[index + 1]).read_text(encoding="utf-8")
                for index, value in enumerate(command)
                if value == "--values"
            )
            document = {
                "apiVersion": "v1",
                "kind": "Namespace" if self.cluster_scoped_router else "Service",
                "metadata": {"name": "router"},
            }
            return _result(argv, stdout=yaml.safe_dump(document))
        if command[:3] == ["kubectl", "api-resources", "--output=wide"]:
            return _result(argv, stdout=_discovery_table(_DISCOVERY))
        if "--dry-run=server" in command:
            return _result(argv, stdout="dry run accepted")
        if command[:4] == ["kubectl", "get", "configmap", "llmd-inventory-demo"]:
            if self.inventory is None:
                return _result(argv, returncode=1, stderr="Error from server (NotFound): configmaps not found")
            return _result(argv, stdout=json.dumps(self.inventory))
        if command[:2] == ["kubectl", "get"] and command[-1] == "--output=name":
            resource = self.resources.get(command[2])
            if resource is not None:
                return _result(argv, stdout=f"{command[2]}\n")
            return _result(argv, returncode=1, stderr="Error from server (NotFound): resource not found")
        if command[:2] == ["kubectl", "get"] and command[-1] == "--output=json":
            resource = self.resources.get(command[2])
            if resource is not None:
                return _result(argv, stdout=json.dumps(resource))
            return _result(argv, returncode=1, stderr="Error from server (NotFound): resource not found")
        if command[:3] == ["kubectl", "apply", "--namespace"]:
            documents = list(yaml.safe_load_all(input or ""))
            inventory = next(
                (
                    document
                    for document in documents
                    if isinstance(document, dict) and document.get("kind") == "ConfigMap"
                ),
                None,
            )
            if inventory is not None:
                self.inventory = inventory
            else:
                for document in documents:
                    identity = {
                        ("apps/v1", "Deployment"): "deployment.apps",
                        ("v1", "PersistentVolumeClaim"): "persistentvolumeclaim",
                    }.get((document.get("apiVersion"), document.get("kind")))
                    if identity:
                        self.resources[f"{identity}/{document['metadata']['name']}"] = document
            return _result(argv, stdout="applied")
        if command[:2] == ["helm", "status"]:
            if self.release_installed:
                return _result(argv, stdout=json.dumps({"info": {"status": "deployed"}}))
            return _result(argv, returncode=1, stderr="Error: release: not found")
        if command[:2] == ["helm", "install"]:
            self.helm_values.extend(
                Path(command[index + 1]).read_text(encoding="utf-8")
                for index, value in enumerate(command)
                if value == "--values"
            )
            self.release_installed = True
            return _result(argv, stdout="ok")
        if command[:2] == ["helm", "uninstall"]:
            self.release_installed = False
            return _result(argv, stdout="ok")
        if command[:2] == ["git", "-C"]:
            return _result(argv, stdout="a" * 40 + "\n")
        if command[:2] == ["kubectl", "delete"]:
            self.resources.pop(command[2], None)
            return _result(argv, stdout="deleted")
        raise AssertionError(f"unexpected command: {command}")


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


@pytest.mark.asyncio
async def test_capabilities_detect_registered_accelerator_and_platform(tmp_path):
    class CapabilityRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            command = list(argv)
            self.commands.append(command)
            self.inputs.append(input)
            if command[:2] == ["kubectl", "version"]:
                return _result(argv, stdout=json.dumps({"serverVersion": {"gitVersion": "v1.34.1-gke.1"}}))
            if command[:3] == ["kubectl", "get", "nodes"]:
                return _result(
                    argv,
                    stdout=json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {
                                        "labels": {
                                            "feature.node.kubernetes.io/pci-10de.present": "true",
                                            "cloud.google.com/gke-nodepool": "workers",
                                        }
                                    },
                                    "status": {"capacity": {"nvidia.com/gpu": "8"}},
                                }
                            ]
                        }
                    ),
                )
            if command[:3] == ["kubectl", "get", "resourceslices.resource.k8s.io"]:
                return _result(argv, stdout=json.dumps({"items": []}))
            if command[:3] == ["kubectl", "api-resources", "--api-group=config.openshift.io"]:
                return _result(argv)
            return await super().__call__(argv, input=input, timeout=timeout)

    capabilities = await NamespaceDeployer(_source(tmp_path), runner=CapabilityRunner()).capabilities()

    assert capabilities.accelerators == ("gpu",)
    assert capabilities.accelerator_status == "detected"
    assert capabilities.platform == "gke"
    assert capabilities.platform_label == "Google Kubernetes Engine"
    assert capabilities.recommended_profile == "gke"


@pytest.mark.asyncio
async def test_capabilities_map_openshift_to_the_upstream_base_profile(tmp_path):
    class OpenShiftRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            command = list(argv)
            self.commands.append(command)
            self.inputs.append(input)
            if command[:2] == ["kubectl", "version"]:
                return _result(argv, stdout=json.dumps({"serverVersion": {"gitVersion": "v1.34.1"}}))
            if command[:3] in (
                ["kubectl", "get", "nodes"],
                ["kubectl", "get", "resourceslices.resource.k8s.io"],
            ):
                return _result(argv, stdout=json.dumps({"items": []}))
            if command[:3] == ["kubectl", "api-resources", "--api-group=config.openshift.io"]:
                return _result(argv, stdout="clusterversions config.openshift.io false ClusterVersion\n")
            return await super().__call__(argv, input=input, timeout=timeout)

    capabilities = await NamespaceDeployer(_source(tmp_path), runner=OpenShiftRunner()).capabilities()

    assert capabilities.platform == "openshift"
    assert capabilities.platform_label == "Red Hat OpenShift"
    assert capabilities.recommended_profile == "base"


@pytest.mark.asyncio
async def test_capabilities_keep_multiple_registered_accelerators_manual(tmp_path):
    class MultipleRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            command = list(argv)
            self.commands.append(command)
            self.inputs.append(input)
            if command[:2] == ["kubectl", "version"]:
                return _result(argv, stdout=json.dumps({"serverVersion": {"gitVersion": "v1.34.1"}}))
            if command[:3] == ["kubectl", "get", "nodes"]:
                return _result(argv, stdout=json.dumps({"items": [{"status": {"capacity": {"nvidia.com/gpu": "8"}}}]}))
            if command[:3] == ["kubectl", "get", "resourceslices.resource.k8s.io"]:
                return _result(argv, stdout=json.dumps({"items": [{"spec": {"driver": "gpu.intel.com"}}]}))
            if command[:3] == ["kubectl", "api-resources", "--api-group=config.openshift.io"]:
                return _result(argv)
            return await super().__call__(argv, input=input, timeout=timeout)

    capabilities = await NamespaceDeployer(_source(tmp_path), runner=MultipleRunner()).capabilities()

    assert capabilities.accelerators == ("gpu", "xpu")
    assert capabilities.accelerator_status == "multiple"
    assert capabilities.accelerator_reason == (
        "Multiple registered accelerator types were detected; choose one manually."
    )


def test_local_capabilities_degrade_on_rbac_denial_without_exposing_cluster_details(tmp_path):
    class DeniedRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            command = list(argv)
            self.commands.append(command)
            self.inputs.append(input)
            if command[:2] == ["kubectl", "version"]:
                return _result(argv, returncode=1, stderr="request unavailable")
            if command[:3] in (
                ["kubectl", "get", "nodes"],
                ["kubectl", "get", "resourceslices.resource.k8s.io"],
            ):
                return _result(argv, returncode=1, stderr="Error from server (Forbidden)")
            if command[:3] == ["kubectl", "api-resources", "--api-group=config.openshift.io"]:
                return _result(argv, returncode=1, stderr="Error from server (Forbidden)")
            return await super().__call__(argv, input=input, timeout=timeout)

    app = create_local_app(
        NamespaceDeployer(_source(tmp_path), runner=DeniedRunner()),
        web_root=tmp_path / "unbuilt",
    )

    response = TestClient(app, base_url="http://localhost").get("/api/local/v1/cluster-capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "accelerators": [],
        "acceleratorStatus": "permission-limited",
        "acceleratorReason": "Accelerator detection needs read access to Nodes or ResourceSlices; choose one manually.",
        "platform": None,
        "platformLabel": None,
        "platformStatus": "permission-limited",
        "platformReason": "Platform detection is permission-limited; choose a profile manually.",
        "recommendedProfile": None,
    }
    assert "Forbidden" not in response.text


@pytest.mark.asyncio
async def test_capabilities_degrade_when_kubectl_is_unavailable(tmp_path):
    async def unavailable_runner(argv, *, input=None, timeout=None):  # noqa: A002, ARG001
        raise FileNotFoundError("kubectl is unavailable")

    capabilities = await NamespaceDeployer(_source(tmp_path), runner=unavailable_runner).capabilities()

    assert capabilities.accelerator_status == "unavailable"
    assert capabilities.platform_status == "unavailable"


def test_catalog_keeps_undeclared_guide_overlay_disabled(tmp_path):
    source = _source(tmp_path)
    (source / "guides" / "optimized-baseline" / "guide.yaml").write_text("name: optimized-baseline\n", encoding="utf-8")

    [entry] = discover_guide_overlays(source)

    assert entry.supported is False
    assert entry.reason == "Guide support matrix is unavailable"


@pytest.mark.asyncio
async def test_catalog_and_render_accept_guide_declared_engine(tmp_path):
    source = _source(tmp_path / "vllm")
    custom = source / "guides" / "optimized-baseline" / "modelserver" / "gpu" / "custom" / "base"
    custom.mkdir(parents=True)
    (custom / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    (source / "guides" / "optimized-baseline" / "guide.yaml").write_text(
        """
support:
  engines: {custom: Custom}
  accelerators:
    gpu:
      model: example/model
      engines: {custom: validated}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    engine_values = source / "guides" / "optimized-baseline" / "router" / "optimized-baseline-custom.values.yaml"
    engine_values.write_text("router: {engine: custom}\n", encoding="utf-8")
    runner = FakeRunner()
    deployer = NamespaceDeployer(source, runner=runner)

    entry = next(item for item in deployer.catalog() if item.engine == "custom")
    await deployer.render(
        namespace="team-a",
        name="demo",
        guide=entry.guide,
        overlay=entry.overlay,
    )

    assert entry.supported is True
    assert any(str(engine_values) in command for command in runner.commands if command[:2] == ["helm", "template"])


@pytest.mark.asyncio
async def test_render_binds_namespace_labels_and_retains_pvc(tmp_path):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)

    plan = await deployer.render(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )

    assert [item.kind for item in plan.resources] == ["Deployment"]
    assert plan.model == "example/model"
    assert [item.kind for item in plan.retained] == ["PersistentVolumeClaim"]
    documents = list(yaml.safe_load_all(plan.model_manifest))
    assert documents[0]["metadata"]["namespace"] == "team-a"
    assert documents[0]["metadata"]["labels"][DEPLOYMENT_LABEL] == "demo"
    assert documents[0]["spec"]["template"]["metadata"]["labels"][DEPLOYMENT_LABEL] == "demo"
    assert any("--dry-run=server" in command for command in runner.commands)
    assert any(
        "router.modelServers.matchLabels.llm-d\\.ai/deployment=demo" in command
        for command in runner.commands
        if command[:2] == ["helm", "template"]
    )


@pytest.mark.asyncio
async def test_render_overrides_guide_model_references_and_router_values(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path / "scratch"))
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)

    plan = await deployer.render(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
        model="acme/new-model",
    )

    [deployment, _claim] = list(yaml.safe_load_all(plan.model_manifest))
    assert plan.model == "acme/new-model"
    assert deployment["metadata"]["labels"]["llm-d.ai/model"] == "new-model"
    assert deployment["spec"]["template"]["metadata"]["labels"]["llm-d.ai/model"] == "new-model"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["args"] == ["acme/new-model"]
    assert any("acme/new-model" in values for values in runner.helm_values)
    assert all("example/model" not in values for values in runner.helm_values)


@pytest.mark.asyncio
async def test_render_rejects_invalid_hugging_face_model_id(tmp_path):
    deployer = NamespaceDeployer(_source(tmp_path), runner=FakeRunner())

    with pytest.raises(DomainValidationError, match="Hugging Face model ID"):
        await deployer.render(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
            model="not a model",
        )


@pytest.mark.asyncio
async def test_render_rejects_custom_model_when_overlay_has_no_safe_patch_point(tmp_path):
    class MissingModelRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            if list(argv)[:2] == ["kubectl", "kustomize"]:
                return _result(
                    argv,
                    stdout=yaml.safe_dump(
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "model-server"},
                            "spec": {"template": {"metadata": {}, "spec": {"containers": []}}},
                        }
                    ),
                )
            return await super().__call__(argv, input=input, timeout=timeout)

    deployer = NamespaceDeployer(_source(tmp_path), runner=MissingModelRunner())

    with pytest.raises(DomainValidationError, match="safe override"):
        await deployer.render(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
            model="acme/new-model",
        )


@pytest.mark.asyncio
async def test_render_refuses_cluster_scoped_router_resource(tmp_path):
    deployer = NamespaceDeployer(_source(tmp_path), runner=FakeRunner(cluster_scoped_router=True))

    with pytest.raises(DomainValidationError, match="cluster-scoped v1 Namespace/router"):
        await deployer.render(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
        )


@pytest.mark.asyncio
async def test_deploy_and_teardown_delete_exact_owned_resources_only(tmp_path):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    inventory = await deployer.deploy(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )

    assert inventory.source_revision == "a" * 40
    assert [item.kind for item in inventory.retained] == ["PersistentVolumeClaim"]
    assert any("--atomic" in command for command in runner.commands if command[:2] == ["helm", "install"])
    result = await deployer.teardown("team-a", "demo")

    assert result["status"] == "removed"
    delete_commands = [command for command in runner.commands if command[:2] == ["kubectl", "delete"]]
    assert any("deployment.apps/model-server" in command for command in delete_commands)
    assert not any("persistentvolumeclaim/model-weights" in command for command in delete_commands)
    assert not any(len(command) > 2 and command[2] == "namespace" for command in delete_commands)


@pytest.mark.asyncio
async def test_status_reports_missing_router_release_as_degraded(tmp_path):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    await deployer.deploy(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )
    runner.release_installed = False

    status = await deployer.status("team-a", "demo")

    assert status["status"] == "degraded"
    assert status["missingResources"] == 0
    assert status["releasePresent"] is False
    assert status["releaseStatus"] is None


@pytest.mark.asyncio
async def test_deploy_refuses_to_adopt_an_existing_rendered_resource(tmp_path):
    class ExistingResourceRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            if "deployment.apps/model-server" in argv and "get" in argv:
                return _result(argv, stdout="deployment.apps/model-server\n")
            return await super().__call__(argv, input=input, timeout=timeout)

    runner = ExistingResourceRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)

    with pytest.raises(ConflictError, match="Deployment/model-server already exists"):
        await deployer.deploy(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
        )

    assert not any(command[:2] == ["helm", "install"] for command in runner.commands)


@pytest.mark.asyncio
async def test_deploy_refuses_to_adopt_an_existing_helm_release(tmp_path):
    class ExistingReleaseRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            if list(argv)[:2] == ["helm", "status"]:
                return _result(argv, stdout=json.dumps({"name": "llmd-demo"}))
            return await super().__call__(argv, input=input, timeout=timeout)

    runner = ExistingReleaseRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)

    with pytest.raises(ConflictError, match="Helm release 'llmd-demo' already exists"):
        await deployer.deploy(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
        )

    assert not any(command[:2] == ["helm", "install"] for command in runner.commands)


@pytest.mark.asyncio
async def test_teardown_refuses_resource_without_stack_ownership_labels(tmp_path):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    await deployer.deploy(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )
    runner.resources["deployment.apps/model-server"]["metadata"]["labels"].pop(DEPLOYMENT_LABEL)

    with pytest.raises(ConflictError, match="no longer carries deployment 'demo' ownership labels"):
        await deployer.teardown("team-a", "demo")

    assert not any(command[:2] == ["helm", "uninstall"] for command in runner.commands)


@pytest.mark.asyncio
async def test_teardown_refuses_cluster_scoped_identity_in_tampered_inventory(tmp_path):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    await deployer.deploy(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )
    payload = json.loads(runner.inventory["data"]["inventory.json"])
    payload["resources"] = [{"apiVersion": "v1", "kind": "Namespace", "name": "team-a"}]
    runner.inventory["data"]["inventory.json"] = json.dumps(payload)

    with pytest.raises(DomainValidationError, match="inventory contains cluster-scoped v1 Namespace/team-a"):
        await deployer.teardown("team-a", "demo")

    assert not any(command[:2] == ["helm", "uninstall"] for command in runner.commands)
    assert not any(command[:2] == ["kubectl", "delete"] for command in runner.commands)


@pytest.mark.asyncio
async def test_discovery_failure_stops_before_dry_run(tmp_path):
    class EmptyDiscoveryRunner(FakeRunner):
        async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
            if list(argv)[:3] == ["kubectl", "api-resources", "--output=wide"]:
                return _result(argv, stdout="")
            return await super().__call__(argv, input=input, timeout=timeout)

    runner = EmptyDiscoveryRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)

    with pytest.raises(DeploymentCommandError, match="discovery returned no resources"):
        await deployer.render(
            namespace="team-a",
            name="demo",
            guide="optimized-baseline",
            overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
        )


def test_local_app_serves_mode_info_without_database(tmp_path):
    source = _source(tmp_path)
    app = create_local_app(NamespaceDeployer(source, runner=FakeRunner()), web_root=tmp_path / "unbuilt")

    response = TestClient(app, base_url="http://localhost").get("/api/local/v1/info")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "local",
        "source": str(source),
        "database": False,
        "connection": {
            "source": "ambient",
            "context": None,
            "contexts": [],
            "ephemeral": False,
            "activeConnectionId": "ambient",
            "connections": [
                {
                    "id": "ambient",
                    "label": "Current kubectl context",
                    "source": "ambient",
                    "context": None,
                    "contexts": [],
                    "ephemeral": False,
                    "active": True,
                }
            ],
        },
    }


def test_local_app_passes_model_selection_to_shared_render(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path / "scratch"))
    runner = FakeRunner()
    app = create_local_app(NamespaceDeployer(_source(tmp_path), runner=runner), web_root=tmp_path / "unbuilt")

    response = TestClient(app, base_url="http://localhost").post(
        "/api/local/v1/render",
        json={
            "namespace": "team-a",
            "name": "demo",
            "guide": "optimized-baseline",
            "overlay": "guides/optimized-baseline/modelserver/gpu/vllm/base",
            "model": "acme/new-model",
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "acme/new-model"
    assert any("acme/new-model" in values for values in runner.helm_values)


def test_local_app_keeps_multiple_ephemeral_kubeconfigs_and_switches_context(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(scratch))
    app = create_local_app(
        NamespaceDeployer(_source(tmp_path), runner=FakeRunner()),
        web_root=tmp_path / "unbuilt",
    )
    kubeconfig = b"""\
apiVersion: v1
kind: Config
clusters:
  - name: local
    cluster: {}
contexts:
  - name: team-a
    context: {cluster: local}
  - name: team-b
    context: {cluster: local}
current-context: team-a
"""

    with TestClient(app, base_url="http://localhost") as client:
        imported = client.post(
            "/api/local/v1/connection",
            files={"file": ("config", kubeconfig, "application/yaml")},
        )

        assert imported.status_code == 200
        imported_info = imported.json()
        first_connection_id = imported_info["activeConnectionId"]
        assert first_connection_id.startswith("uploaded-")
        assert {key: imported_info[key] for key in ("source", "context", "contexts", "ephemeral")} == {
            "source": "uploaded",
            "context": "team-a",
            "contexts": ["team-a", "team-b"],
            "ephemeral": True,
        }
        assert len(imported_info["connections"]) == 2
        assert imported_info["connections"][-1]["label"] == "config"
        assert "clusters" not in imported.text
        first_path = Path(app.state.local_connection.current_deployer().kubeconfig)
        assert first_path.read_bytes() == kubeconfig
        assert stat.S_IMODE(first_path.stat().st_mode) == 0o600

        second = client.post(
            "/api/local/v1/connection",
            files={"file": ("another-config", kubeconfig, "application/yaml")},
        )
        second_connection_id = second.json()["activeConnectionId"]
        second_path = Path(app.state.local_connection.current_deployer().kubeconfig)
        assert second_connection_id != first_connection_id
        assert len(second.json()["connections"]) == 3

        selected = client.post(
            "/api/local/v1/connection/context",
            json={"context": "team-b", "connectionId": first_connection_id},
        )
        assert selected.status_code == 200
        assert selected.json()["context"] == "team-b"
        assert selected.json()["activeConnectionId"] == first_connection_id
        assert app.state.local_connection.current_deployer().context == "team-b"

        removed = client.delete(f"/api/local/v1/connection/{second_connection_id}")
        assert removed.status_code == 200
        assert len(removed.json()["connections"]) == 2
        assert removed.json()["activeConnectionId"] == first_connection_id

        startup_remove = client.delete("/api/local/v1/connection/ambient")
        assert startup_remove.status_code == 422
        assert startup_remove.json()["detail"] == "The startup or ambient connection cannot be removed"

    assert not first_path.exists()
    assert not second_path.exists()


def test_local_app_rejects_invalid_kubeconfig_without_replacing_connection(tmp_path):
    app = create_local_app(
        NamespaceDeployer(_source(tmp_path), runner=FakeRunner()),
        web_root=tmp_path / "unbuilt",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/local/v1/connection",
            files={"file": ("config", b"kind: Config\ncontexts: []\n", "application/yaml")},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"
        assert client.get("/api/local/v1/info").json()["connection"]["source"] == "ambient"


def test_local_app_rejects_cross_origin_kubeconfig_import(tmp_path):
    app = create_local_app(
        NamespaceDeployer(_source(tmp_path), runner=FakeRunner()),
        web_root=tmp_path / "unbuilt",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/local/v1/connection",
            files={"file": ("config", b"kind: Config\n", "application/yaml")},
            headers={"origin": "https://example.invalid"},
        )

        assert response.status_code == 403
        assert client.get("/api/local/v1/info").json()["connection"]["source"] == "ambient"


def test_local_app_rejects_oversized_kubeconfig(tmp_path):
    app = create_local_app(
        NamespaceDeployer(_source(tmp_path), runner=FakeRunner()),
        web_root=tmp_path / "unbuilt",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/local/v1/connection",
            files={"file": ("config", b"x" * (1024 * 1024 + 1), "application/yaml")},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "Kubeconfig must not exceed 1 MiB"


def test_local_app_rejects_non_loopback_host_header(tmp_path):
    app = create_local_app(NamespaceDeployer(_source(tmp_path), runner=FakeRunner()), web_root=tmp_path / "unbuilt")

    response = TestClient(app, base_url="http://localhost").get("/api/local/v1/info", headers={"host": "example.test"})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_cli_deploy_uses_shared_namespace_deployer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path / "scratch"))
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    monkeypatch.setattr("llm_d_bench.deploy.cli._deployer", lambda _args: deployer)

    result = cli_main(
        [
            "deploy",
            "--namespace",
            "team-a",
            "--name",
            "demo",
            "--guide",
            "optimized-baseline",
            "--overlay",
            "guides/optimized-baseline/modelserver/gpu/vllm/base",
            "--model",
            "acme/new-model",
            "--output",
            "json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "installed"
    assert payload["model"] == "acme/new-model"
    assert any(command[:2] == ["helm", "install"] for command in runner.commands)


def test_cli_catalog_defaults_to_deployable_overlays_and_all_explains_exclusions(tmp_path, capsys):
    source = _source(tmp_path)
    component = source / "guides" / "optimized-baseline" / "modelserver" / "gpu" / "vllm" / "components" / "shared"
    component.mkdir(parents=True)
    (component / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")

    assert cli_main(["catalog", "--source", str(source)]) == 0
    default_output = capsys.readouterr().out
    assert "DEPLOYER SUPPORT\tUPSTREAM MATURITY\tREASON" in default_output
    assert "gpu/vllm/base\tsupported\tvalidated\t-" in default_output
    assert "components/shared" not in default_output

    assert cli_main(["catalog", "--source", str(source), "--all"]) == 0
    all_output = capsys.readouterr().out
    assert (
        "gpu/vllm/components/shared\tunsupported\tvalidated\toverlay is an internal Kustomize component" in all_output
    )

    assert cli_main(["catalog", "--source", str(source), "--output", "json"]) == 0
    default_json = json.loads(capsys.readouterr().out)
    assert len(default_json) == 1
    assert default_json[0]["supported"] is True

    assert cli_main(["catalog", "--source", str(source), "--all", "--output", "json"]) == 0
    all_json = json.loads(capsys.readouterr().out)
    assert len(all_json) == 2
    excluded = next(item for item in all_json if not item["supported"])
    assert excluded["reason"] == "overlay is an internal Kustomize component"


_GUIDE_STEPS = """
name: optimized-baseline
support:
  engines: {vllm: vLLM}
  accelerators:
    gpu:
      model: example/model
      engines: {vllm: validated}
    npu:
      model: example/model
      engines: {vllm: community}
env:
  static:
    REPO_ROOT: $(git rev-parse --show-toplevel)
    GUIDE_NAME: optimized-baseline
    NAMESPACE: llm-d-optimized-baseline
    MONITORING: {default: "false", values: ["false", "true"]}
    MONITORING_VALUES: {default: ""}
    ACCELERATOR_TYPE: {default: gpu, values: [gpu, npu]}
    MODEL_SERVER: {default: vllm, values: [vllm]}
    INFRA_PROVIDER: {default: base, values: [base]}
prerequisites:
  gaie:
    - run: kubectl apply -f https://example.invalid/crds.yaml
  namespace:
    - run: kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
  secrets:
    - skip_in: [ci]
      run: kubectl create secret generic llm-d-hf-token -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
deploy:
  router_values:
    - run: |
        export ROUTER_BASE_VALUES="${REPO_ROOT}/guides/recipes/router/base.values.yaml"
        export ROUTER_VALUES="${REPO_ROOT}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml"
  monitoring_values:
    - when: {MONITORING: ["true"]}
      run: export MONITORING_VALUES="-f ${REPO_ROOT}/guides/recipes/router/features/monitoring.values.yaml"
  accelerator_values:
    - when: {ACCELERATOR_TYPE: [npu]}
      run: export ACCELERATOR_VALUES="-f ${REPO_ROOT}/guides/${GUIDE_NAME}/router/npu.values.yaml"
  standalone:
    - run: |
        helm install ${GUIDE_NAME} \\
          ${ROUTER_STANDALONE_CHART} \\
          -f ${ROUTER_BASE_VALUES} \\
          ${MONITORING_VALUES} \\
          -f ${ROUTER_VALUES} \\
          ${ACCELERATOR_VALUES:-} \\
          -n ${NAMESPACE} --version ${ROUTER_CHART_VERSION}
  gateway:
    - run: helm install ${GUIDE_NAME} ${ROUTER_GATEWAY_CHART} -n ${NAMESPACE}
  modelserver:
    - run: |
        kubectl apply -n ${NAMESPACE} \\
          -k ${REPO_ROOT}/guides/${GUIDE_NAME}/modelserver/${ACCELERATOR_TYPE}/${MODEL_SERVER}/${INFRA_PROVIDER}/
  render:
    - when: {ACCELERATOR_TYPE: [npu]}
      run: kubectl apply -n ${NAMESPACE} -k ${REPO_ROOT}/guides/${GUIDE_NAME}/render/
  monitoring:
    - when: {MONITORING: ["true"]}
      run: kubectl apply -n ${NAMESPACE} -k ${REPO_ROOT}/guides/recipes/modelserver/components/monitoring
"""


def _guide_steps_source(tmp_path: Path) -> Path:
    source = _source(tmp_path)
    guide_root = source / "guides" / "optimized-baseline"
    (guide_root / "guide.yaml").write_text(_GUIDE_STEPS, encoding="utf-8")
    npu = guide_root / "modelserver" / "npu" / "vllm" / "base"
    npu.mkdir(parents=True)
    (npu / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    (guide_root / "router" / "npu.values.yaml").write_text("router: {npu: true}\n", encoding="utf-8")
    render = guide_root / "render"
    render.mkdir()
    (render / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    (source / "guides" / "env.sh").write_text(
        "export ROUTER_CHART_VERSION=${ROUTER_CHART_VERSION:-v9}\n"
        "export ROUTER_STANDALONE_CHART=${ROUTER_STANDALONE_CHART:-oci://example.invalid/router}\n",
        encoding="utf-8",
    )
    return source


class GuideStepsRunner(FakeRunner):
    """Renders a distinct Service for the Guide's extra render Kustomization."""

    async def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
        command = list(argv)
        if command[:2] == ["kubectl", "kustomize"] and command[2].endswith("/render"):
            self.commands.append(command)
            self.inputs.append(input)
            document = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "render"}}
            return _result(argv, stdout=yaml.safe_dump(document))
        return await super().__call__(argv, input=input, timeout=timeout)


def _helm_values(command: list[str]) -> list[str]:
    return [Path(command[index + 1]).name for index, value in enumerate(command) if value == "--values"]


@pytest.mark.asyncio
async def test_render_follows_guide_declared_router_values_and_chart(tmp_path):
    runner = GuideStepsRunner()
    deployer = NamespaceDeployer(_guide_steps_source(tmp_path), runner=runner)

    await deployer.render(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/npu/vllm/base",
    )

    [template] = [command for command in runner.commands if command[:2] == ["helm", "template"]]
    assert template[3:4] == ["oci://example.invalid/router"]
    assert template[template.index("--version") + 1] == "v9"
    assert _helm_values(template) == ["base.values.yaml", "optimized-baseline.values.yaml", "npu.values.yaml"]
    assert "router.monitoring.prometheus.auth.enabled=false" in template


@pytest.mark.asyncio
async def test_deploy_applies_and_inventories_guide_declared_namespaced_manifests(tmp_path):
    runner = GuideStepsRunner()
    deployer = NamespaceDeployer(_guide_steps_source(tmp_path), runner=runner)

    inventory = await deployer.deploy(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/npu/vllm/base",
    )

    assert [f"{item.kind}/{item.name}" for item in inventory.resources] == ["Deployment/model-server", "Service/render"]
    applied = [
        document
        for command, stdin in zip(runner.commands, runner.inputs, strict=True)
        if command[:3] == ["kubectl", "apply", "--namespace"]
        for document in yaml.safe_load_all(stdin or "")
        if document and document.get("kind") == "Service"
    ]
    assert applied[0]["metadata"]["labels"][DEPLOYMENT_LABEL] == "demo"
    helm_commands = [" ".join(command) for command in runner.commands if command[0] == "helm"]
    assert not any("ROUTER_GATEWAY_CHART" in command for command in helm_commands)


@pytest.mark.asyncio
async def test_guide_steps_skip_alternatives_that_do_not_match_the_overlay(tmp_path):
    runner = GuideStepsRunner()
    deployer = NamespaceDeployer(_guide_steps_source(tmp_path), runner=runner)

    plan = await deployer.render(
        namespace="team-a",
        name="demo",
        guide="optimized-baseline",
        overlay="guides/optimized-baseline/modelserver/gpu/vllm/base",
    )

    [template] = [command for command in runner.commands if command[:2] == ["helm", "template"]]
    assert _helm_values(template) == ["base.values.yaml", "optimized-baseline.values.yaml"]
    assert [item.kind for item in plan.resources] == ["Deployment"]


def test_catalog_rejects_guide_step_that_changes_the_namespace_outside_kubectl_apply(tmp_path):
    source = _guide_steps_source(tmp_path)
    guide = source / "guides" / "optimized-baseline" / "guide.yaml"
    guide.write_text(
        guide.read_text(encoding="utf-8")
        + "  storage:\n"
        + "    - run: envsubst < ${REPO_ROOT}/pvc.yaml | kubectl apply -n ${NAMESPACE} -f -\n",
        encoding="utf-8",
    )

    entries = {entry.accelerator: entry for entry in discover_guide_overlays(source)}

    assert entries["gpu"].supported is False
    assert entries["gpu"].reason == (
        "guide.yaml step deploy.storage changes the cluster with a command this deployer cannot follow"
    )


def test_cli_list_prints_stack_inventories_not_catalog_rows(tmp_path, monkeypatch, capsys):
    runner = FakeRunner()
    deployer = NamespaceDeployer(_source(tmp_path), runner=runner)
    monkeypatch.setattr("llm_d_bench.deploy.cli._deployer", lambda _args: deployer)
    deploy = ["deploy", "--namespace", "team-a", "--name", "demo", "--guide", "optimized-baseline"]
    assert cli_main([*deploy, "--overlay", "guides/optimized-baseline/modelserver/gpu/vllm/base"]) == 0
    capsys.readouterr()
    runner.commands.clear()

    async def list_inventories(argv, *, input=None, timeout=None):  # noqa: A002
        if list(argv)[:3] == ["kubectl", "get", "configmaps"]:
            return _result(argv, stdout=json.dumps({"items": [runner.inventory]}))
        return await FakeRunner.__call__(runner, argv, input=input, timeout=timeout)

    deployer._runner = list_inventories

    assert cli_main(["list", "--namespace", "team-a"]) == 0
    assert capsys.readouterr().out == "team-a/demo\tinstalled\n"


def test_local_ui_refuses_non_loopback_bind(tmp_path, capsys):
    result = cli_main(["ui", "--source", str(_source(tmp_path)), "--host", "0.0.0.0"])  # noqa: S104

    assert result == 2
    assert "must bind to a loopback address" in capsys.readouterr().err
