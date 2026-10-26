from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from llm_d_bench.core.exceptions import ConflictError, DomainValidationError
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
