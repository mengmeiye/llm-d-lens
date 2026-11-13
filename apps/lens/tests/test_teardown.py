import pytest

from lens import inventory, lifecycle
from lens.cli import main
from lens.errors import PolicyError

from .test_status import cluster, stood_up  # noqa: F401 - the cluster fixture

CLAIM = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "name": "cache",
    "resource": "persistentvolumeclaims",
}


def test_teardown_removes_exactly_what_the_instance_owns(llmrepo, cluster):  # noqa: F811
    kube = stood_up(llmrepo, cluster)
    cluster.put("team-a", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "unrelated"}})
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["deleted"] == ["Deployment/model-a", "Service/model-a"]
    assert cluster.get("team-a", "deployments.apps", "model-a") is None
    assert cluster.names("team-a", "pods") == []  # gone with their Deployment
    assert cluster.get("team-a", "configmaps", "unrelated") is not None
    assert inventory.read(kube, "team-a", "r1") is None


def test_claims_are_kept_unless_asked_for(llmrepo, cluster):  # noqa: F811
    kube = stood_up(llmrepo, cluster)
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "cache"}}
    stored = cluster.put("team-a", claim)
    stored["metadata"]["labels"] = {
        "app.kubernetes.io/managed-by": "llm-d-lens",
        "lens.llm-d.ai/instance": "r1",
    }
    record = inventory.require(kube, "team-a", "r1")
    objects = [*record["objects"], {**CLAIM, "uid": stored["metadata"]["uid"]}]
    inventory.write(kube, {**record, "objects": objects})
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["kept"] == ["PersistentVolumeClaim/cache"]
    assert inventory.require(kube, "team-a", "r1")["phase"] == "Removed"
    assert lifecycle.teardown(kube, "team-a", "r1", delete_volumes=True)["deleted"] == ["PersistentVolumeClaim/cache"]
    assert inventory.read(kube, "team-a", "r1") is None


def test_an_object_replaced_or_taken_over_is_skipped(llmrepo, cluster):  # noqa: F811
    kube = stood_up(llmrepo, cluster)
    cluster.get("team-a", "services", "model-a")["metadata"]["uid"] = "recreated"
    cluster.get("team-a", "deployments.apps", "model-a")["metadata"]["labels"] = {}
    result = lifecycle.teardown(kube, "team-a", "r1")
    assert result["deleted"] == [] and sorted(result["skipped"]) == ["Deployment/model-a", "Service/model-a"]
    assert cluster.get("team-a", "services", "model-a") is not None


def test_teardown_checks_delete_permission_before_anything(llmrepo, cluster):  # noqa: F811
    kube = stood_up(llmrepo, cluster)
    cluster.denied = {("delete", "deployments.apps")}
    with pytest.raises(PolicyError, match="nothing was removed"):
        lifecycle.teardown(kube, "team-a", "r1")
    assert inventory.require(kube, "team-a", "r1")["phase"] == "Applied"


def test_teardown_refuses_a_record_that_lists_a_cluster_scoped_object(llmrepo, cluster):  # noqa: F811
    # llmd records namespaced objects only. A record edited by hand to list a cluster-scoped one, with its
    # UID and the instance's labels, is refused before anything is removed.
    kube = stood_up(llmrepo, cluster)
    cluster.serve("example.com/v1", "ClusterWidget", "clusterwidgets", namespaced=False)
    labels = {"app.kubernetes.io/managed-by": "llm-d-lens", "lens.llm-d.ai/instance": "r1"}
    widget = {"apiVersion": "example.com/v1", "kind": "ClusterWidget", "metadata": {"name": "w", "labels": labels}}
    stored = cluster.put("", widget)
    record = inventory.require(kube, "team-a", "r1")
    entry = {
        "apiVersion": "example.com/v1",
        "kind": "ClusterWidget",
        "name": "w",
        "resource": "clusterwidgets.example.com",
        "uid": stored["metadata"]["uid"],
    }
    inventory.write(kube, {**record, "objects": [*record["objects"], entry]})
    with pytest.raises(PolicyError, match="nothing was removed"):
        lifecycle.teardown(kube, "team-a", "r1")
    assert cluster.get("", "clusterwidgets.example.com", "w") is not None
    assert cluster.get("team-a", "deployments.apps", "model-a") is not None
    assert inventory.require(kube, "team-a", "r1")["phase"] == "Applied"


def test_forget_leaves_every_object(llmrepo, cluster):  # noqa: F811
    kube = stood_up(llmrepo, cluster)
    assert lifecycle.teardown(kube, "team-a", "r1", forget=True)["forgot"]
    assert cluster.get("team-a", "deployments.apps", "model-a") is not None
    assert inventory.read(kube, "team-a", "r1") is None


def test_teardown_command_asks_unless_yes(llmrepo, cluster, tmp_path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("lens.commands.RUNNER", cluster)
    stood_up(llmrepo, cluster)
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "teardown", "r1"]) == 2  # no terminal to confirm
    assert main([*state, "teardown", "r1", "-y"]) == 0
    assert "Removed 2 object(s) of r1" in capsys.readouterr().out
