"""The kubectl/oc adapter. Every call names the one target namespace.

* API discovery uses ``get --raw /api/v1``, ``/apis/<group>`` and ``/apis/<group>/<version>``, which
  every authenticated user may read; it gives each kind's scope and plural name.
* Permission checks submit SelfSubjectAccessReviews in one ``create -f -`` call.
* Writes stay in the target namespace: ``apply`` refuses a cluster-scoped object or one that
  names another namespace, and ``delete`` refuses any resource discovery does not report as
  namespaced, since kubectl ignores ``-n`` for a cluster-scoped one.
* Objects llmd creates are applied with server-side apply under the ``llm-d-lens`` field
  manager. Objects of an imported stack are applied the way the team applies them
  (``kubectl apply``, client-side), so the team's own ``kubectl apply`` keeps working.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlio
from .errors import CommandError, NotFoundError, PolicyError
from .model import FIELD_MANAGER, api_group
from .process import Result, Runner, subprocess_runner

IN_CLUSTER_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


@dataclass(frozen=True)
class ApiResource:
    group: str
    version: str
    kind: str
    plural: str
    namespaced: bool

    @property
    def qualified(self) -> str:
        """The unambiguous kubectl resource name, e.g. ``deployments.apps``."""
        return f"{self.plural}.{self.group}" if self.group else self.plural


@dataclass(frozen=True)
class Target:
    """Which credentials to use. Empty fields mean kubectl's own defaults."""

    kubeconfig: str = ""
    context: str = ""
    cli: str = "kubectl"


def guess_plural(kind: str) -> str:
    """A kind's resource name when discovery is not available, by kubectl's own fallback rule."""
    singular = kind.lower()
    if singular.endswith("s"):
        return singular + "es"
    if singular.endswith("y"):
        return singular[:-1] + "ies"
    return singular + "s"


class Kube:
    def __init__(self, target: Target | None = None, runner: Runner = subprocess_runner) -> None:
        self.target = target or Target()
        self._runner = runner
        self._discovery: dict[str, dict[str, ApiResource] | None] = {}
        #: API group -> the versions the cluster serves for it.
        self._group_versions: dict[str, list[str]] = {}
        #: What ``prefetch`` read, (namespace, resource, name) -> object or None, until ``forget``.
        self._objects: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        #: Called with a short line before each slow step (the CLI prints it on stderr).
        self.progress: Callable[[str], None] = lambda line: None

    # -- plumbing ------------------------------------------------------------------------------------
    def base(self) -> list[str]:
        argv = [self.target.cli]
        if self.target.kubeconfig:
            argv += ["--kubeconfig", self.target.kubeconfig]
        if self.target.context:
            argv += ["--context", self.target.context]
        return argv

    def run(self, *args: str, stdin: str | None = None, timeout: float = 120) -> Result:
        return self._runner([*self.base(), *args], stdin=stdin, timeout=timeout)

    # -- identity ------------------------------------------------------------------------------------
    def current_context(self) -> str:
        if self.target.context:
            return self.target.context
        result = self.run("config", "current-context", timeout=30)
        return result.stdout.strip() if result.ok else ""

    def server(self) -> str:
        """The API server URL of the selected context (read from the kubeconfig, no cluster call)."""
        if not self.target.kubeconfig and not self.target.context and _in_cluster():
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
            host = f"[{host}]" if ":" in host else host
            return f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}" if host else ""
        result = self.run("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}", timeout=30)
        return result.stdout.strip() if result.ok else ""

    def default_namespace(self) -> str:
        if not self.target.kubeconfig and not self.target.context and _in_cluster():
            return IN_CLUSTER_NAMESPACE.read_text(encoding="utf-8").strip()
        result = self.run("config", "view", "--minify", "-o", "jsonpath={..namespace}", timeout=30)
        return result.stdout.strip() if result.ok else ""

    def whoami(self) -> str:
        result = self.run("auth", "whoami", "-o", "json", timeout=30)
        try:
            return str(json.loads(result.stdout)["status"]["userInfo"]["username"]) if result.ok else ""
        except (ValueError, KeyError, TypeError):
            return ""

    # -- discovery -----------------------------------------------------------------------------------
    def resources(self, api_version: str) -> dict[str, ApiResource] | None:
        """Kind -> resource for one group/version, or None when the cluster does not serve it."""
        if api_version in self._discovery:
            return self._discovery[api_version]
        path = f"/apis/{api_version}" if "/" in api_version else f"/api/{api_version}"
        result = self.run("get", "--raw", path, timeout=60)
        found: dict[str, ApiResource] | None = None
        if result.ok:
            group, version = api_group(api_version), api_version.split("/", 1)[-1]
            found = {}
            for item in json.loads(result.stdout or "{}").get("resources") or []:
                if "/" not in item.get("name", ""):  # leave out subresources such as deployments/scale
                    found[item["kind"]] = ApiResource(
                        group, version, item["kind"], item["name"], bool(item.get("namespaced"))
                    )
        elif "NotFound" not in result.message and "could not find" not in result.message:
            raise CommandError(f"API discovery for {api_version} failed: {result.message}")
        self._discovery[api_version] = found
        return found

    def resource(self, api_version: str, kind: str) -> ApiResource | None:
        return (self.resources(api_version) or {}).get(kind)

    def resource_name(self, document: dict[str, Any]) -> str:
        """The kubectl resource name of an object's kind (discovery first, kubectl's rule otherwise)."""
        found = self.resource(document["apiVersion"], document["kind"])
        if found:
            return found.qualified
        group = api_group(document["apiVersion"])
        plural = guess_plural(document["kind"])
        return f"{plural}.{group}" if group else plural

    def scope(self, api_version: str, kind: str) -> bool | None:
        """True when namespaced, False when cluster-scoped, None when the cluster does not serve it."""
        found = self.resource(api_version, kind)
        return None if found is None else found.namespaced

    def namespaced(self, resource: str, api_version: str = "") -> bool | None:
        """Whether a ``plural.group`` resource is namespaced, by discovery: at ``api_version`` (the
        version a record names) when it is of that group, else at each version the cluster serves for
        the group, since a kind has one scope in all its versions. None when the cluster serves none."""
        plural, _, group = resource.partition(".")

        def at(version: str) -> bool | None:
            found = [item for item in (self.resources(version) or {}).values() if item.plural == plural]
            return found[0].namespaced if found else None

        if api_version and api_group(api_version) == group and (answer := at(api_version)) is not None:
            return answer
        for version in self._served_versions(group):
            if (answer := at(version)) is not None:
                return answer
        return None

    def _served_versions(self, group: str) -> list[str]:
        """The group/versions the cluster serves for an API group (``v1`` for the core group)."""
        if not group:
            return ["v1"]
        if group not in self._group_versions:
            result = self.run("get", "--raw", f"/apis/{group}", timeout=60)
            if result.ok:
                versions = json.loads(result.stdout or "{}").get("versions") or []
                self._group_versions[group] = [item["groupVersion"] for item in versions if item.get("groupVersion")]
            elif "NotFound" in result.message or "could not find" in result.message:
                self._group_versions[group] = []
            else:
                raise CommandError(f"API discovery for {group} failed: {result.message}")
        return self._group_versions[group]

    # -- reads ---------------------------------------------------------------------------------------
    def access_reviews(self, checks: list[tuple[str, str, str, str]]) -> list[bool | None]:
        """[(verb, group, plural, namespace)] -> allowed? (None when a review was not answered)."""
        if not checks:
            return []
        items = [
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"verb": verb, "group": group, "resource": plural, "namespace": ns}},
            }
            for verb, group, plural, ns in checks
        ]
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        result = self.run("create", "-o", "json", "-f", "-", stdin=body, timeout=60)
        if not result.ok:
            return [None] * len(checks)
        try:
            reviews = yamlio.kubectl_objects(result.stdout)
        except ValueError:
            return [None] * len(checks)
        allowed = [
            bool((review.get("status") or {}).get("allowed")) if "allowed" in (review.get("status") or {}) else None
            for review in reviews
        ]
        return allowed + [None] * (len(checks) - len(allowed))

    def namespace_state(self, namespace: str) -> str:
        """exists | missing | unknown (a team member may not be allowed to read Namespace objects)."""
        result = self.run("get", "namespace", namespace, "-o", "name", timeout=30)
        if result.ok:
            return "exists"
        return "missing" if "NotFound" in result.message else "unknown"

    def get_json(
        self,
        resource: str,
        namespace: str,
        *,
        names: list[str] | None = None,
        selector: str = "",
        field_selector: str = "",
    ) -> list[dict[str, Any]]:
        args = ["get", resource, *(names or []), "-n", namespace, "-o", "json", "--ignore-not-found"]
        if selector:
            args += ["-l", selector]
        if field_selector:
            args += ["--field-selector", field_selector]
        result = self.run(*args, timeout=60)
        if not result.ok:
            if "NotFound" in result.message or "doesn't have a resource type" in result.message:
                return []
            raise CommandError(f"reading {resource} in {namespace} failed: {result.message}")
        return yamlio.kubectl_objects(result.stdout)

    def previous_log(self, pod: str, container: str, namespace: str, *, lines: int = 20) -> str | None:
        """The last ``lines`` lines a container wrote before its last restart, or None when they cannot
        be read (no permission, or the log is gone)."""
        result = self.run("logs", pod, "-c", container, "-n", namespace, "--previous", f"--tail={lines}", timeout=30)
        return result.stdout if result.ok else None

    def get(self, resource: str, name: str, namespace: str) -> dict[str, Any] | None:
        key = (namespace, resource, name)
        if key in self._objects:
            return self._objects[key]
        found = self.get_json(resource, namespace, names=[name])
        return found[0] if found else None

    def prefetch(self, items: list[tuple[str, str]], namespace: str) -> None:
        """Read many objects with one ``kubectl get`` per resource, so the ``get`` calls that follow
        for them are answered from that read. ``items`` are (resource, name). A resource whose read
        fails is left to ``get``, which raises the error for the object that needs it. ``forget``,
        and any apply or delete, drops what was read, so nothing reads an object from before a write."""
        wanted: dict[str, list[str]] = {}
        for resource, name in items:
            names = wanted.setdefault(resource, [])
            if (namespace, resource, name) not in self._objects and name not in names:
                names.append(name)
        for resource, names in wanted.items():
            for start in range(0, len(names), _NAMES_PER_GET):
                chunk = names[start : start + _NAMES_PER_GET]
                try:
                    found = self.get_json(resource, namespace, names=chunk)
                except CommandError:
                    continue
                by_name = {(item.get("metadata") or {}).get("name"): item for item in found}
                for name in chunk:
                    self._objects[(namespace, resource, name)] = by_name.get(name)

    def forget(self) -> None:
        """Drop what ``prefetch`` read, so the next ``get`` reads the cluster again."""
        self._objects.clear()

    # -- writes --------------------------------------------------------------------------------------
    def apply(
        self,
        documents: list[dict[str, Any]],
        namespace: str,
        *,
        server_side: bool = True,
        dry_run: bool = False,
        force_conflicts: bool = False,
        timeout: float = 300,
    ) -> list[dict[str, Any]]:
        """Apply objects. ``server_side`` uses server-side apply as llmd's field manager; without it,
        ``kubectl apply`` as a team applies its YAML (the last-applied annotation is kept)."""
        for document in documents:
            self._require_in(namespace, document)
        if not dry_run:
            self._objects.clear()
        args = ["apply", "-n", namespace, "-o", "json", "-f", "-"]
        if server_side:
            args[1:1] = ["--server-side", f"--field-manager={FIELD_MANAGER}"]
            if force_conflicts:
                args.insert(1, "--force-conflicts")
        if dry_run:
            args.insert(1, "--dry-run=server")
        result = self.run(*args, stdin=yamlio.dump_documents(documents), timeout=timeout)
        if not result.ok:
            raise CommandError(
                f"{'server dry run' if dry_run else 'apply'} failed: {result.message}",
                hint="Missing permissions, a quota, or an admission policy can cause this.",
            )
        return yamlio.kubectl_objects(result.stdout)

    def delete(self, resource: str, name: str, namespace: str, *, wait: bool = False, api_version: str = "") -> bool:
        """Delete one object of ``namespace``. ``api_version`` is the version a record names, where
        discovery looks first for whether ``resource`` is namespaced."""
        if self.namespaced(resource, api_version) is not True:
            raise PolicyError(f"refusing to delete {resource}/{name}: it is not a namespaced resource")
        self._objects.clear()
        result = self.run(
            "delete",
            resource,
            name,
            "-n",
            namespace,
            "--ignore-not-found",
            f"--wait={'true' if wait else 'false'}",
            timeout=600 if wait else 120,
        )
        if not result.ok:
            if "NotFound" in result.message:
                return False
            raise CommandError(f"deleting {resource}/{name} failed: {result.message}")
        return bool(result.stdout.strip())

    def _require_in(self, namespace: str, document: dict[str, Any]) -> None:
        """Refuse an object that would land outside ``namespace``."""
        metadata = document.get("metadata") or {}
        ref = f"{document['kind']}/{metadata.get('name')}"
        if metadata.get("namespace") and metadata["namespace"] != namespace:
            raise PolicyError(f"refusing to apply {ref}: it names namespace {metadata['namespace']}")
        if self.scope(document["apiVersion"], document["kind"]) is False:
            raise PolicyError(f"refusing to apply {ref}: it is cluster-scoped")

    def require(self, resource: str, name: str, namespace: str) -> dict[str, Any]:
        found = self.get(resource, name, namespace)
        if found is None:
            raise NotFoundError(f"{resource}/{name} not found in {namespace}")
        return found


#: Names per ``kubectl get`` in a prefetch: well inside any command-line length limit.
_NAMES_PER_GET = 100


def _in_cluster() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and IN_CLUSTER_NAMESPACE.is_file()
