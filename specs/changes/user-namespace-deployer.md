# Namespace-scoped llm-d deployer and local Lens entrance

Status: implemented prototype for review on `feat/user-cli`

## Summary

Lens should provide one deployment engine with two entrances:

- `llmd`, a scriptable CLI for deploying, inspecting, and tearing down an
  llm-d stack in an existing namespace; and
- a lightweight local Lens entrance for users who have a kubeconfig and
  namespace permissions but do not administer a Lens server.

Both entrances use the same Python domain service, request fields, safety
policy, Guide discovery, rendered manifests, and ownership inventory. The local
entrance reuses the existing React application, HTTP transport, polling hook,
and UI primitives. It is a runtime mode, not a copied frontend or a separate
product directory.

Here, "non-admin" means a local operator whose kubeconfig grants sufficient
RBAC in one existing namespace. It does not broaden the hosted Lens `end-user`
role or bypass the hosted application's authentication and authorization.

The first version intentionally has no database, user directory, saved cluster
registry, benchmarking, model cache, namespace provisioning, or infrastructure
installation. It binds only to a loopback interface and uses the
process's standard kubeconfig/current context by default. The browser may
add one or more uploaded kubeconfigs, but the files are scoped to the local
process, never persisted in a database, and removed on shutdown.

## Goals

1. Let a namespace-scoped user render, deploy, inspect, and remove one or more
   identifiable llm-d stacks in namespaces to which that user already has
   access.
2. Make the CLI and local UX equivalent projections of one deployment contract.
3. Consume manifests and router values from an llm-d source checkout instead of
   maintaining scenario files or copies of upstream YAML in Lens.
4. Make teardown precise and recoverable: delete only resources recorded for
   the named stack and retain data-bearing or user-supplied resources.
5. Fail before mutation when a rendered plan contains cluster-scoped objects or
   objects targeting another namespace.

## Non-goals for the first version

- creating, deleting, labeling, or otherwise managing namespaces;
- installing CRDs, operators, GatewayClasses, StorageClasses, cluster roles,
  device plugins, drivers, monitoring, or platform security policy;
- administering users, sessions, shared kubeconfigs, or credentials;
- downloading models ahead of deployment or requiring hostPath or NFS;
- benchmarking, capacity planning, optimization, or topology generation;
- translating arbitrary Guide shell commands into privileged automation; and
- claiming that every upstream Guide is deployable by a namespace-scoped user.

## Findings from the existing implementations

Lens already renders Guide Kustomize overlays, installs router charts, validates
namespace names, restricts command shapes, and tracks deployment lifecycle.
Those lower-level conventions should be reused. The current deployment service
also creates and deletes whole namespaces and is composed with persistent
cluster, authentication, and deployment records, so its lifecycle boundary is
not safe for this entrance.

The [llm-d-benchmark](https://github.com/llm-d/llm-d-benchmark) standup
implementation provides a useful command/engine separation and readiness
sequencing. Its scenario-file contract and broad resource cleanup are
intentionally not adopted. The deployer instead discovers upstream Kustomize
entry points and the Guide's own deploy steps, and writes a namespaced,
stack-specific inventory.

## Shared architecture

```text
React local entrance       llmd CLI
          |                    |
          +--- local API ------+
                    |
          NamespaceDeployer service
          /       |            \
 Guide catalog  policy      inventory
          \       |            /
       kubectl kustomize / kubectl / Helm
                    |
          one existing namespace
```

The CLI calls `NamespaceDeployer` directly. The local FastAPI router adapts the
same service for the browser. No database model is introduced.

### Source and catalog

The source is an explicit local llm-d checkout supplied by `--source`, by
`LLM_D_ROOT`, or by local checkout discovery. Source resolution accepts only a
directory containing `guides/`; it does not execute Guide documentation.

The catalog discovers `guides/<guide>/modelserver/**/kustomization.yaml` and
records paths relative to the checkout. A deployable stack also requires a
Guide support matrix declaring the accelerator and engine combination.

Each Guide's `guide.yaml` is the upstream machine-readable source of its
commands. When its `deploy` section names model-server overlays, the deployer
follows those steps for the selected overlay instead of guessing from the
directory layout:

- the `deploy.standalone` Helm install gives the router chart, its version
  (resolved from `guides/env.sh`), and every values file the Guide layers,
  including accelerator- and engine-specific files selected by `when:`
  filters;
- every `kubectl apply -n ${NAMESPACE} -k|-f <path>` step that applies to the
  overlay, such as a render pool, a Kueue LocalQueue, or a model-cache PVC, is
  rendered, labeled, and inventoried with the stack;
- alternative model-server steps, Gateway Mode, and monitoring steps are not
  selected (monitoring needs a cluster monitoring stack);
- cluster-wide `prerequisites` (CRDs, operators) and the Guide's namespace and
  Secret creation are left to the administrator and the user; and
- any other step that changes the cluster in a way the deployer cannot follow
  without a shell (for example `envsubst | kubectl apply`) makes the overlay
  unsupported with that reason.

The deployer never runs Guide shell commands. Guides without such `deploy`
steps keep the older convention: `guides/recipes/router/base.values.yaml`, a
Guide-owned router values file, and the router chart pinned by Lens.

Catalog entries that lack this contract remain visible as unsupported with an
explanation. Parent Kustomize bases that contain more specific overlays also
remain disabled to avoid presenting duplicate or internal entry points.

The local UI projects deployable catalog leaves into four cascading choices:

1. **Serving architecture** (`guide`)
2. **Accelerator**
3. **Inference engine**
4. **Deployment profile** (the remaining overlay path, which may represent a
   platform, topology, network, storage, or connector choice)

The final profile still resolves to the same authoritative `guide` and `overlay`
request fields used by the CLI. Guide, accelerator, engine, and provider labels
come from `guide.yaml`; Lens does not maintain another label or scenario catalog.
TensorRT-LLM is therefore available only when upstream metadata declares the
`trtllm` engine for the selected architecture and accelerator.

The local UI also requests advisory cluster capabilities. Accelerator detection
matches Node labels, extended resources, and DRA ResourceSlice drivers against
the existing hardware profile registry. Platform detection uses normalized,
read-only Kubernetes distribution signals and recommends an exact profile only
when the match is unambiguous. These probes return no node data or credential
material. Cluster-scoped reads are commonly forbidden to namespace operators, so
permission denial, unknown hardware, and multiple accelerators all preserve full
manual selection and never block render or deployment.

OpenShift detection recommends the Guide's `base` profile. The UI presents that
existing value as **OpenShift (Kubernetes base overlay)** without creating a
synthetic overlay, because current Guide metadata does not declare a separate
OpenShift manifest. Server-side preflight remains authoritative, and Lens does
not grant SCC access or install cluster prerequisites.

### Render and namespace policy

`kubectl kustomize` produces the model-server documents and any other
namespaced manifests the Guide applies. `helm template` produces the router
documents before install. The router is rendered with
`router.monitoring.prometheus.auth.enabled=false`, because authenticated
`/metrics` makes the chart create a ClusterRole and ClusterRoleBinding for
TokenReview and SubjectAccessReview. The deployer parses every document and
applies these rules:

1. Require `apiVersion`, `kind`, and `metadata.name`.
2. Query Kubernetes API discovery and reject every cluster-scoped kind.
3. Reject an explicit `metadata.namespace` different from the requested
   namespace; bind an omitted namespace to the requested namespace.
4. Add stack identity and managed-by labels to namespaced resources and Pod
   templates, including LeaderWorkerSet leader and worker templates, so the
   stack-specific router selector matches every serving Pod.
5. Never add a Namespace, persistent volume, StorageClass, CRD, ClusterRole,
   ClusterRoleBinding, SCC, webhook, or another cluster-scoped prerequisite.
6. Run a server-side dry-run before the first mutation. A dry-run stores
   nothing, so a RoleBinding to a Role from the same plan would fail the RBAC
   escalation check for users without the `bind` verb. Those bindings are
   checked with `kubectl auth can-i create rolebindings` instead; their Roles
   are still dry-run, which proves the user holds the granted rules. Actual
   installation retains the repository's client-side apply behavior.

Render output can be inspected with `llmd render`. A failed render or preflight
does not create an inventory.

### Model and storage behavior

The default is the official Guide's runtime model download behavior. The UI
prefills the Guide-declared Hugging Face model ID and the CLI accepts the same
optional `--model organization/model-name` override. A custom ID replaces the
declared full ID and model label in parsed model-server documents and temporary
Helm values, so render, installation, and inventory agree. It does not retune
resources, topology, parser settings, or engine-specific arguments; the user is
responsible for selecting a compatible model. Render rejects a custom ID when
the model-server documents do not expose the Guide-declared full model ID as a
safe patch point.

Lens does not pre-download weights and does not require hostPath or NFS. If the
Guide references a Hugging Face Secret, the Secret must already exist in the
target namespace; the first version does not read a host token or copy
credentials.

PVC support is conservative. Existing PVC references in an official overlay are
allowed, but Lens does not provision a PV, StorageClass, or cross-namespace
claim. PVC and Secret objects are never removed by teardown. A later version can
add an explicit existing-PVC mount contract after the Guides expose a stable
patch point; guessing volume names across engines is unsafe.

### Ownership and teardown

Each stack has a DNS-label name. Applied resources receive:

```yaml
app.kubernetes.io/managed-by: llmd
llm-d.ai/deployment: <stack-name>
```

After successful deployment, the service stores a small ConfigMap named from
the stack in the same namespace. It contains the source revision when available,
Guide, overlay, selected model, Helm release, and exact
`{apiVersion, kind, name}` inventory.
It contains no manifest bodies, kubeconfig data, tokens, or Secret values.

`llmd teardown` reads that ConfigMap, uninstalls the recorded Helm release, and
deletes each recorded object by exact identity after verifying its stack
ownership labels and confirming through API discovery that every identity is
still namespaced. It does not delete by a broad label selector and does not
delete the namespace, PVCs, Secrets, or unrecorded objects. The inventory is
removed last. A missing or malformed inventory, an unverifiable scope, or an
ownership mismatch is a hard refusal with manual recovery instructions;
`--force` must never mean "delete the namespace."

Deploy is also create-only. It refuses an existing inventory, any rendered
object that already exists, or the derived Helm release before mutation. It
does not adopt or update resources that were created outside this deployment
inventory.

### Readiness and status

The first implementation reports applied resources and observes exact recorded
workloads. It does not promise Guide-specific end-to-end health. Status is
derived from the inventory, live namespaced objects, and recorded Helm release
and is therefore shared between `llmd status` and the local page. Helm
installation is atomic so a failed router install rolls back its own partial
resources. The UX polls quietly using the existing polling hook.

## CLI contract

```text
llmd catalog  --source PATH [--all]
llmd render   --source PATH --namespace NS --name NAME --guide GUIDE --overlay PATH [--model HF_ID]
llmd deploy   --source PATH --namespace NS --name NAME --guide GUIDE --overlay PATH [--model HF_ID]
llmd list     --namespace NS
llmd status   --namespace NS --name NAME
llmd teardown --namespace NS --name NAME
llmd ui       --source PATH [--host 127.0.0.1] [--port 8000]
```

Common Kubernetes flags are `--context` and `--kubeconfig`. They are passed to
the standard clients for the current process and are not copied into Lens data.
CLI output defaults to concise text and supports `--output json` for automation.
Errors go to stderr and use a nonzero exit status. `deploy` refuses to replace a
different recorded stack with the same name.

`catalog` lists only deployable overlays by default. `--all` includes internal
Kustomize bases/components and other excluded entries with explicit deployer
support, upstream maturity, and rejection-reason fields. JSON clients use the
`supported` boolean rather than inferring support from maturity.

`render` performs local rendering and policy checks without applying. `deploy`
always performs the same server dry-run before mutation.

## Local Lens entrance

`llmd ui` starts a small FastAPI composition root and serves the existing built
React assets. The default bind is `127.0.0.1`; non-loopback addresses and
non-loopback HTTP `Host` headers are rejected because this mode intentionally
has no multi-user authentication layer.

At runtime the shared `App` detects the local entrance and initializes the same
post-session application shell and left navigation as hosted Lens with an
explicit implicit-local session mode. That mode performs no credential login,
contacts no hosted authentication service, and exposes only the existing
**Clusters** and **Deployments** navigation entries. The Clusters view uses a
database-free adapter around the shared kubeconfig picker and process-local
connection API; it does not load the hosted registry, bootstrap, monitoring,
maintenance, or cluster-admin actions. The existing `DeploymentManagementPage`
selects its namespace-scoped adapter while hosted mode retains its current
service and API. The local flow contains:

- a Clusters page for importing kubeconfigs, selecting a context, and handing
  the target to Deployments;
- source revision and cascading architecture, accelerator, engine, and deployment
  profile selectors from the shared Guide catalog;
- namespace and stack-name inputs;
- a render/preflight action that shows planned resources and retained-resource
  policy;
- a deploy action;
- a polling list/status panel; and
- a confirmation dialog for exact teardown.

All mutations use the existing JSON transport. The namespace variant reuses
the existing deployment shell, summary cards, responsive deployment table,
form, panel, button, badge, empty-state, loading, and confirmation primitives.
Hosted-only row actions such as sharing, metadata editing, pod logs, monitoring,
and port forwarding are absent because the local API does not provide them.

## Local API

The local API is intentionally separate from the authenticated, database-backed
server composition while remaining in the deployment domain:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/local/v1/info` | Runtime mode, source, and credential-free connection summary |
| `POST` | `/api/local/v1/connection` | Import a kubeconfig into process-temporary storage |
| `POST` | `/api/local/v1/connection/context` | Select an imported/startup connection and one of its declared contexts |
| `DELETE` | `/api/local/v1/connection/{connectionId}` | Forget one process-local kubeconfig import without changing the cluster |
| `GET` | `/api/local/v1/catalog` | Discovered Guide overlays |
| `GET` | `/api/local/v1/cluster-capabilities` | Non-sensitive advisory accelerator and platform recommendations |
| `POST` | `/api/local/v1/render` | Render and preflight a plan |
| `GET` | `/api/local/v1/deployments?namespace=...` | List inventory ConfigMaps |
| `GET` | `/api/local/v1/deployments/{name}?namespace=...` | Read exact status |
| `POST` | `/api/local/v1/deployments` | Deploy a plan |
| `DELETE` | `/api/local/v1/deployments/{name}?namespace=...` | Exact teardown |

The request model for render and deploy is the same as the CLI options,
including the optional Hugging Face `model` ID. Domain
errors map to problem-details responses. The browser may upload kubeconfig
contents, but it can never submit an arbitrary server-side path or executable.
The local adapter validates each upload, writes it to a process-owned `0600`
temporary file for `kubectl` and Helm, and removes every imported file on
shutdown. Its multi-connection catalog exists only in memory. It never imports
the hosted cluster registry or stores kubeconfig contents in a database or
persistent Lens data. The local source remains fixed when the process starts.

## Security and privilege assumptions

The user must already have, in the designated namespace:

- create/patch/update/get/delete permission for every rendered namespaced kind,
  including the router's Roles, RoleBindings, and InferencePool;
- the rules the router's Roles grant (read access to Pods, InferencePools,
  InferenceObjectives, and InferenceModelRewrites), because Kubernetes only
  lets a user create a Role whose rules that user already holds;
- permission to create and read the inventory ConfigMap;
- permission for Helm's namespaced release Secrets when a router is installed;
  and
- access to any referenced Secret or PVC as required by admission/runtime.

The built-in `admin` ClusterRole bound with a namespaced RoleBinding covers the
core kinds. Custom resources such as InferencePool, DisaggregatedSet, and
LeaderWorkerSet need their rules aggregated into that role (or granted another
way) by a cluster administrator.

The cluster administrator must already have installed every required CRD,
operator, device/runtime integration, Gateway API component, and platform policy.
On OpenShift, the namespace must already admit the rendered service accounts and
Pod security context; Lens does not grant SCC use. On managed Kubernetes, the
required accelerator drivers, node pools, and admission controllers must already
exist.

Open questions are reported as preflight failures rather than handled with more
privilege:

- Some Guides combine namespaced custom resources with cluster-scoped setup in
  documentation. The deployer can apply the former only after discovery proves
  the CRDs already exist.
- Some router modes depend on a preexisting Gateway/GatewayClass. The first
  version uses the standalone router convention when available and does not
  install Gateway infrastructure.
- Provider admission can impose restrictions that are not visible in static
  YAML. Server dry-run is authoritative, and Lens should display its sanitized
  denial without retrying with broader access.
- Accelerator auto-detection may require cluster-scoped Node or ResourceSlice
  reads that a namespace operator does not have. The UI must keep manual choices
  available and must not request broader access merely to improve a default.
- Upstream Guide layouts are evolving. Unsupported layouts must be labeled with
  the missing convention; adding support should extend the shared catalog and
  render contract, never add a UX-only deployment path.
- Guides that declare `deploy` steps also pin their router chart version
  through `guides/env.sh`, so the router stays compatible with that source
  revision's router values. Guides without such steps still use Lens's pinned
  chart; for them a source upgrade requires maintainer validation and must
  fail visibly when render or preflight detects drift.

## Delivery phases

### Phase 1: minimal shared deployer

- dynamic catalog for standard Guide model-server overlays;
- the Guide's own `guide.yaml` router and namespaced apply steps when declared;
- render, cluster-scope rejection, namespace binding, labeling, and preflight;
- router template/install when the standard upstream values contract exists;
- ConfigMap inventory, list/status, and exact teardown;
- CLI, loopback local API, and shared React entrance; and
- mocked lifecycle tests, plus a manual check of render, deploy, status, and
  teardown on a local kind cluster as a user bound to the namespaced `admin`
  role.

### Later phases

- Guide-owned machine-readable deployment descriptors;
- stable existing-PVC mount parameters;
- richer workload readiness and logs;
- model-aware compatibility checks and engine-specific tuning; and
- benchmarking and capacity guidance.

These additions must remain optional and must use the same service contract for
CLI and UX.
