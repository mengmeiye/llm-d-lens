import pytest

from lens.errors import CommandError, NotFoundError, PolicyError
from lens.kube import Kube, Target, guess_plural

from .fakes import FakeCluster

CONFIGMAP = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}, "data": {"k": "v"}}


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def kube(cluster):
    return Kube(Target(context="ctx"), runner=cluster)


def test_every_call_names_the_context(kube, cluster):
    kube.get("configmaps", "a", "team-a")
    assert cluster.calls[-1][:2] == ["get", "configmaps"] and "-n" in cluster.calls[-1]


def test_discovery_gives_scope_and_plural(kube):
    assert kube.resource_name({"apiVersion": "apps/v1", "kind": "Deployment"}) == "deployments.apps"
    assert kube.scope("v1", "PersistentVolume") is False
    assert kube.scope("v1", "Service") is True
    assert kube.scope("example.invalid/v1", "Widget") is None
    assert kube.resource_name({"apiVersion": "example.invalid/v1", "kind": "NetworkPolicy"}) == (
        "networkpolicies.example.invalid"
    )


def test_guess_plural():
    assert [guess_plural(kind) for kind in ("Ingress", "NetworkPolicy", "Route")] == [
        "ingresses",
        "networkpolicies",
        "routes",
    ]


def test_server_side_apply_and_client_side_apply(kube, cluster):
    kube.apply([CONFIGMAP], "team-a")
    stored = cluster.get("team-a", "configmaps", "a")
    assert "kubectl.kubernetes.io/last-applied-configuration" not in stored["metadata"].get("annotations", {})
    assert "--server-side" in cluster.calls[-1]
    kube.apply([CONFIGMAP], "team-a", server_side=False)
    assert "--server-side" not in cluster.calls[-1]
    stored = cluster.get("team-a", "configmaps", "a")
    assert "kubectl.kubernetes.io/last-applied-configuration" in stored["metadata"]["annotations"]


def test_a_dry_run_changes_nothing(kube, cluster):
    kube.apply([CONFIGMAP], "team-a", dry_run=True)
    assert cluster.get("team-a", "configmaps", "a") is None
    assert "--dry-run=server" in cluster.calls[-1]


def test_a_refused_apply_is_a_command_error(kube, cluster):
    cluster.denied.add(("create", "configmaps"))
    with pytest.raises(CommandError, match="Forbidden"):
        kube.apply([CONFIGMAP], "team-a")


def test_writes_stay_in_the_namespace(kube, cluster):
    elsewhere = {**CONFIGMAP, "metadata": {"name": "a", "namespace": "team-b"}}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "r"}}
    for document in (elsewhere, role):
        with pytest.raises(PolicyError, match="refusing to apply"):
            kube.apply([CONFIGMAP, document], "team-a", dry_run=True)
    for resource in ("clusterroles.rbac.authorization.k8s.io", "persistentvolumes", "namespaces"):
        with pytest.raises(PolicyError, match="refusing to delete"):
            kube.delete(resource, "x", "team-a")
    assert not [call for call in cluster.calls if call[0] in ("apply", "delete")]


def test_delete_asks_discovery_whether_a_resource_is_namespaced(kube, cluster):
    # kubectl ignores -n for a cluster-scoped resource, so delete refuses one, by the cluster's own answer.
    cluster.serve("example.com/v1", "ClusterWidget", "clusterwidgets", namespaced=False)
    cluster.serve("example.com/v2", "Widget", "widgets")
    cluster.put("team-a", {"apiVersion": "example.com/v2", "kind": "Widget", "metadata": {"name": "w"}})
    for api_version in ("example.com/v1", "v1", ""):  # recorded right, recorded as another group's, not given
        with pytest.raises(PolicyError, match="refusing to delete"):
            kube.delete("clusterwidgets.example.com", "x", "team-a", api_version=api_version)
    with pytest.raises(PolicyError, match="refusing to delete"):
        kube.delete("gadgets.example.com", "x", "team-a")  # a resource the cluster does not serve
    assert not [call for call in cluster.calls if call[0] == "delete"]
    # A namespaced kind recorded at a version the cluster no longer serves is found through its group.
    assert kube.delete("widgets.example.com", "w", "team-a", api_version="example.com/v1alpha1") is True


def test_access_reviews(kube, cluster):
    cluster.denied.add(("delete", "deployments.apps"))
    checks = [("create", "", "configmaps", "team-a"), ("delete", "apps", "deployments", "team-a")]
    assert kube.access_reviews(checks) == [True, False]


def test_reads_and_deletes(kube, cluster):
    cluster.put("team-a", {**CONFIGMAP, "metadata": {"name": "a", "labels": {"app": "x"}}})
    assert [item["metadata"]["name"] for item in kube.get_json("configmaps", "team-a", selector="app=x")] == ["a"]
    assert kube.get_json("configmaps", "team-a", selector="app=y") == []
    assert kube.require("configmaps", "a", "team-a")["metadata"]["uid"]
    assert kube.delete("configmaps", "a", "team-a") is True
    assert kube.delete("configmaps", "a", "team-a") is False
    with pytest.raises(NotFoundError):
        kube.require("configmaps", "a", "team-a")


def test_identity(kube):
    assert kube.whoami() == "alice"
    assert kube.current_context() == "ctx"
    assert kube.namespace_state("team-a") == "exists" and kube.namespace_state("other") == "missing"
