import { useCallback, useEffect, useMemo, useState } from 'react';
import { Network, PackageOpen, Rocket, ShieldCheck } from 'lucide-react';

import { usePolling } from '../../hooks/usePolling';
import { errorMessage } from '../../utils/errorMessage';
import {
    Badge,
    Button,
    DeleteConfirmation,
    EmptyState,
    FormError,
    Input,
    Label,
    LoadingState,
    ModuleHeader,
    ModulePage,
    Panel,
    Select,
} from '../ui';
import { DeploymentSummary } from './DeploymentSummary';
import { DeploymentTable } from './DeploymentTable';
import { LocalModeHeaderActions } from '../shared/LocalModeHeaderActions';
import { SectionLabel } from '../ui/SectionLabel';
import { summarizeStatuses } from './deploymentPresentation';
import {
    deploymentCatalogFacets,
    onlyOption,
    presentedDeploymentProfiles,
    recommendedAccelerator,
    recommendedDeploymentProfile,
} from './deploymentCatalog.js';
import {
    deployNamespaceDeployment,
    getNamespaceClusterCapabilities,
    getNamespaceDeploymentInfo,
    getNamespaceDeploymentStatus,
    getNamespaceGuideCatalog,
    listNamespaceDeployments,
    renderNamespaceDeployment,
    teardownNamespaceDeployment,
} from './namespaceDeploymentBackend.js';

const NAMESPACE = /^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$/;
const NAME = /^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$/;
const MODEL_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const NAMESPACE_STORAGE_KEY = 'llmd_namespace';
const LOCAL_CONTEXT_ID = 'local-kubernetes-context';

function deploymentItem(item, catalogByOverlay) {
    const catalog = catalogByOverlay.get(item.overlay);
    return {
        ...item,
        execution_id: `${item.namespace}/${item.name}`,
        display_name: item.name,
        description: item.overlay,
        model: item.model || catalog?.model || '',
        backend: catalog?.engine || '',
        cluster_id: LOCAL_CONTEXT_ID,
    };
}

export function NamespaceDeploymentView({ onNavigate, onToggleMobileNav } = {}) {
    const [info, setInfo] = useState(null);
    const [catalog, setCatalog] = useState([]);
    const [capabilities, setCapabilities] = useState(null);
    const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
    const [namespace, setNamespace] = useState(() => sessionStorage.getItem(NAMESPACE_STORAGE_KEY) || '');
    const [name, setName] = useState('');
    const [guide, setGuide] = useState('');
    const [accelerator, setAccelerator] = useState('');
    const [engine, setEngine] = useState('');
    const [overlay, setOverlay] = useState('');
    const [model, setModel] = useState('');
    const [deployments, setDeployments] = useState([]);
    const [plan, setPlan] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [submitting, setSubmitting] = useState('');
    const [error, setError] = useState('');
    const [deleteTarget, setDeleteTarget] = useState(null);

    const selected = useMemo(() => catalog.find((item) => item.overlay === overlay) || null, [catalog, overlay]);
    const facets = useMemo(
        () => deploymentCatalogFacets(catalog, { guide, accelerator, engine }),
        [accelerator, catalog, engine, guide]
    );
    const profileOptions = useMemo(
        () => presentedDeploymentProfiles(facets.profiles, capabilities),
        [capabilities, facets.profiles]
    );
    const detectedAccelerator = useMemo(
        () => recommendedAccelerator(facets.accelerators, capabilities),
        [capabilities, facets.accelerators]
    );
    const detectedProfile = useMemo(
        () => recommendedDeploymentProfile(profileOptions, capabilities),
        [capabilities, profileOptions]
    );
    const catalogByOverlay = useMemo(() => new Map(catalog.map((item) => [item.overlay, item])), [catalog]);
    const namespaceValid = namespace.length <= 63 && NAMESPACE.test(namespace);
    const nameValid = name.length <= 47 && NAME.test(name);
    const modelValid = model.length <= 96 && MODEL_ID.test(model) && !model.includes('--') && !model.includes('..');
    const modelSuggestions = useMemo(
        () => [...new Set(catalog
            .filter((item) => item.supported && item.guide === guide && item.accelerator === accelerator && item.engine === engine)
            .map((item) => item.model)
            .filter(Boolean))],
        [accelerator, catalog, engine, guide]
    );
    const request = selected && namespaceValid && nameValid && modelValid
        ? { namespace, name, guide: selected.guide, overlay: selected.overlay, model }
        : null;
    const items = useMemo(
        () => deployments.map((item) => deploymentItem(item, catalogByOverlay)),
        [catalogByOverlay, deployments]
    );
    const summary = useMemo(() => summarizeStatuses(items), [items]);
    const activeConnection = useMemo(
        () => info?.connection?.connections?.find((item) => item.active) || null,
        [info]
    );

    useEffect(() => {
        if (namespaceValid) sessionStorage.setItem(NAMESPACE_STORAGE_KEY, namespace);
    }, [namespace, namespaceValid]);

    useEffect(() => {
        let active = true;
        Promise.all([getNamespaceDeploymentInfo(), getNamespaceGuideCatalog()])
            .then(([localInfo, entries]) => {
                if (!active) return;
                setInfo(localInfo);
                setCatalog(entries);
            })
            .catch((loadError) => active && setError(errorMessage(loadError)))
            .finally(() => active && setLoading(false));
        getNamespaceClusterCapabilities()
            .then((result) => active && setCapabilities(result))
            .catch(() => active && setCapabilities(null))
            .finally(() => active && setCapabilitiesLoading(false));
        return () => { active = false; };
    }, []);

    useEffect(() => {
        if (!guide || accelerator) return;
        const next = detectedAccelerator || onlyOption(facets.accelerators);
        if (next) setAccelerator(next);
    }, [accelerator, detectedAccelerator, facets.accelerators, guide]);

    useEffect(() => {
        if (!accelerator || engine) return;
        const next = onlyOption(facets.engines);
        if (next) setEngine(next);
    }, [accelerator, engine, facets.engines]);

    useEffect(() => {
        if (!engine || overlay) return;
        const next = detectedProfile || onlyOption(profileOptions);
        if (next) setOverlay(next);
    }, [detectedProfile, engine, overlay, profileOptions]);

    useEffect(() => {
        setModel(selected?.model || '');
    }, [selected]);

    const loadDeployments = useCallback(async ({ quiet = false } = {}) => {
        if (!namespaceValid) {
            setDeployments([]);
            return;
        }
        if (!quiet) setRefreshing(true);
        try {
            const listed = await listNamespaceDeployments(namespace);
            const statuses = await Promise.all(listed.map(async (item) => {
                try {
                    return await getNamespaceDeploymentStatus(namespace, item.name);
                } catch {
                    return { ...item, status: 'unreachable' };
                }
            }));
            setDeployments(statuses);
            if (!quiet) setError('');
        } catch (loadError) {
            if (!quiet) setError(errorMessage(loadError));
        } finally {
            if (!quiet) setRefreshing(false);
        }
    }, [namespace, namespaceValid]);

    useEffect(() => {
        loadDeployments();
    }, [loadDeployments]);
    usePolling(() => loadDeployments({ quiet: true }), { enabled: namespaceValid });

    const submit = async (operation) => {
        if (!request || submitting) return;
        setSubmitting(operation);
        setError('');
        try {
            if (operation === 'render') {
                setPlan(await renderNamespaceDeployment(request));
            } else {
                await deployNamespaceDeployment(request);
                setPlan(null);
                setName('');
                await loadDeployments({ quiet: true });
            }
        } catch (submitError) {
            setError(errorMessage(submitError));
        } finally {
            setSubmitting('');
        }
    };

    const confirmTeardown = async (confirmed) => {
        if (!confirmed) {
            setDeleteTarget(null);
            return;
        }
        setSubmitting('teardown');
        setError('');
        try {
            await teardownNamespaceDeployment(deleteTarget.namespace, deleteTarget.name);
            setDeleteTarget(null);
            await loadDeployments({ quiet: true });
        } catch (teardownError) {
            setError(errorMessage(teardownError));
        } finally {
            setSubmitting('');
        }
    };

    if (loading) return <LoadingState fullPage label="Loading deployment catalog" />;

    return (
        <ModulePage contentClassName="flex flex-col gap-4">
            <ModuleHeader
                icon={Rocket}
                title="Deployments"
                description="Deploy and remove llm-d stacks in an existing namespace using your local Kubernetes access."
                onToggleMobileNav={onToggleMobileNav}
                actions={(
                    <LocalModeHeaderActions
                        databaseFree={info?.database === false}
                        onRefresh={() => loadDeployments()}
                        refreshing={refreshing}
                        disabled={!namespaceValid}
                        refreshLabel="Refresh deployments"
                    />
                )}
            />

            <Panel
                title="Target cluster"
                actions={(
                    <Badge tone="neutral">
                        {info?.connection?.source === 'uploaded'
                            ? 'Imported kubeconfig'
                            : info?.connection?.source === 'startup'
                                ? 'Startup kubeconfig'
                                : 'Ambient context'}
                    </Badge>
                )}
            >
                <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 items-center gap-3">
                        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-sky-500/25 bg-sky-500/10 text-sky-400">
                            <Network size={18} aria-hidden="true" />
                        </div>
                        <div className="min-w-0">
                            <p className="truncate text-sm font-semibold text-theme-text">
                                {info?.connection?.context || activeConnection?.label || 'Current kubectl context'}
                            </p>
                            <p className="mt-1 text-xs text-theme-muted">
                                Selected on the Clusters page for this local session.
                            </p>
                        </div>
                    </div>
                    <Button variant="secondary" onClick={() => onNavigate?.('clusters')}>Change cluster</Button>
                </div>
            </Panel>

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(20rem,0.8fr)]">
                <Panel title="Deploy a stack">
                    <div className="grid gap-4 sm:grid-cols-2">
                        <div>
                            <Label htmlFor="namespace-deployments-filter">Namespace</Label>
                            <Input
                                id="namespace-deployments-filter"
                                value={namespace}
                                onChange={(event) => { setNamespace(event.target.value); setPlan(null); }}
                                placeholder="my-namespace"
                                error={namespace && !namespaceValid}
                            />
                            {namespace && !namespaceValid && (
                                <p className="mt-1 text-xs text-red-600 dark:text-red-400">Enter a valid Kubernetes namespace.</p>
                            )}
                        </div>
                        <div>
                            <Label htmlFor="namespace-deployment-name">Deployment name</Label>
                            <Input
                                id="namespace-deployment-name"
                                value={name}
                                onChange={(event) => { setName(event.target.value); setPlan(null); }}
                                placeholder="my-llm-d-stack"
                                error={name && !nameValid}
                            />
                            {name && !nameValid && (
                                <p className="mt-1 text-xs text-red-600 dark:text-red-400">Use a DNS label with at most 47 characters.</p>
                            )}
                        </div>
                        <div>
                            <Label htmlFor="namespace-deployment-guide">Serving architecture</Label>
                            <Select
                                id="namespace-deployment-guide"
                                value={guide}
                                onChange={(event) => {
                                    setGuide(event.target.value);
                                    setAccelerator('');
                                    setEngine('');
                                    setOverlay('');
                                    setModel('');
                                    setPlan(null);
                                }}
                            >
                                <option value="">Select an architecture</option>
                                {facets.architectures.map((option) => (
                                    <option key={option.value} value={option.value}>
                                        {option.label}
                                    </option>
                                ))}
                            </Select>
                        </div>
                        <div>
                            <Label htmlFor="namespace-deployment-accelerator">Accelerator</Label>
                            <Select
                                id="namespace-deployment-accelerator"
                                value={accelerator}
                                disabled={!guide}
                                onChange={(event) => {
                                    setAccelerator(event.target.value);
                                    setEngine('');
                                    setOverlay('');
                                    setModel('');
                                    setPlan(null);
                                }}
                            >
                                <option value="">Select an accelerator</option>
                                {facets.accelerators.map((option) => (
                                    <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                            </Select>
                            <p className="mt-1 text-xs text-theme-muted">
                                {capabilitiesLoading
                                    ? 'Detecting registered accelerator hardware…'
                                    : detectedAccelerator
                                        ? 'Matched registered cluster hardware; you can override it.'
                                        : capabilities?.acceleratorReason || 'Choose the accelerator available to this namespace.'}
                            </p>
                        </div>
                        <div>
                            <Label htmlFor="namespace-deployment-engine">Inference engine</Label>
                            <Select
                                id="namespace-deployment-engine"
                                value={engine}
                                disabled={!accelerator}
                                onChange={(event) => {
                                    setEngine(event.target.value);
                                    setOverlay('');
                                    setModel('');
                                    setPlan(null);
                                }}
                            >
                                <option value="">Select an inference engine</option>
                                {facets.engines.map((option) => (
                                    <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                            </Select>
                        </div>
                        <div>
                            <Label htmlFor="namespace-deployment-overlay">Deployment profile</Label>
                            <Select
                                id="namespace-deployment-overlay"
                                value={overlay}
                                disabled={!engine}
                                onChange={(event) => { setOverlay(event.target.value); setPlan(null); }}
                            >
                                <option value="">Select a deployment profile</option>
                                {profileOptions.map((option) => (
                                    <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                            </Select>
                            <p className="mt-1 text-xs text-theme-muted">
                                {capabilitiesLoading
                                    ? 'Detecting the cluster platform…'
                                    : capabilities?.platform === 'openshift'
                                        ? 'OpenShift uses the Guide\'s Kubernetes base overlay; preflight checks the existing namespace permissions and security policy.'
                                    : capabilities?.platformLabel
                                        ? `Detected ${capabilities.platformLabel}; a matching profile is suggested when unambiguous.`
                                        : capabilities?.platformReason || 'Profiles may also select topology, storage, or network behavior.'}
                            </p>
                        </div>
                        <div className="sm:col-span-2">
                            <Label htmlFor="namespace-deployment-model">Hugging Face model</Label>
                            <Input
                                id="namespace-deployment-model"
                                list="namespace-deployment-model-suggestions"
                                value={model}
                                maxLength={96}
                                disabled={!selected}
                                onChange={(event) => { setModel(event.target.value); setPlan(null); }}
                                placeholder="organization/model-name"
                                error={model && !modelValid}
                            />
                            <datalist id="namespace-deployment-model-suggestions">
                                {modelSuggestions.map((suggestion) => <option key={suggestion} value={suggestion} />)}
                            </datalist>
                            {model && !modelValid ? (
                                <p className="mt-1 text-xs text-red-600 dark:text-red-400">
                                    Enter a Hugging Face model ID such as organization/model-name.
                                </p>
                            ) : (
                                <p className="mt-1 text-xs text-theme-muted">
                                    The Guide default is prefilled. A custom model must be compatible with the selected engine, hardware, and Guide tuning.
                                </p>
                            )}
                        </div>
                    </div>

                    {selected && (
                        <div className="mt-4 flex flex-wrap gap-2 text-xs text-theme-muted">
                            {model && <Badge tone="neutral" size="md">{model}</Badge>}
                            {model && selected.model && model !== selected.model && (
                                <Badge tone="warning" size="md">Custom model override</Badge>
                            )}
                            {selected.maturity && (
                                <Badge tone={selected.maturity === 'validated' ? 'success' : 'warning'} size="md">
                                    {selected.maturity}
                                </Badge>
                            )}
                            <Badge tone="neutral" size="md">Runtime model download</Badge>
                        </div>
                    )}

                    <FormError message={error} className="mt-4" />
                    <div className="mt-5 flex flex-wrap gap-3">
                        <Button
                            variant="secondary"
                            onClick={() => submit('render')}
                            isLoading={submitting === 'render'}
                            disabled={!request || Boolean(submitting)}
                        >
                            Render and preflight
                        </Button>
                        <Button
                            onClick={() => submit('deploy')}
                            isLoading={submitting === 'deploy'}
                            disabled={!request || Boolean(submitting)}
                        >
                            Deploy stack
                        </Button>
                    </div>
                </Panel>

                <Panel title="Namespace safety boundary">
                    <div className="flex items-start gap-3">
                        <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" aria-hidden="true" />
                        <div className="space-y-2 text-sm leading-6 text-theme-muted">
                            <p>Only namespaced objects in the designated namespace are accepted.</p>
                            <p>Namespaces, cluster-scoped prerequisites, PVCs, and Secrets are never removed.</p>
                            <p>Model weights may be downloaded by the deployed workload at runtime.</p>
                        </div>
                    </div>
                </Panel>
            </div>

            {plan && (
                <Panel title="Preflight result" actions={<Badge tone="success">Accepted</Badge>}>
                    <p className="text-sm text-theme-muted">
                        {plan.resources.length} managed resources and {plan.retainedResources.length} retained resources are in scope.
                    </p>
                    <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                        {plan.resources.map((resource) => (
                            <div key={`${resource.apiVersion}/${resource.kind}/${resource.name}`} className="rounded-lg border border-theme-border bg-theme-bg p-3">
                                <p className="text-xs font-semibold text-theme-text">{resource.kind}/{resource.name}</p>
                                <p className="mt-1 text-[11px] text-theme-muted">{resource.apiVersion}</p>
                            </div>
                        ))}
                    </div>
                </Panel>
            )}

            <DeploymentSummary summary={summary} />

            <section
                aria-busy={refreshing}
                className="relative rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 shadow-2xl backdrop-blur-xl"
            >
                <div className="relative overflow-visible p-6">
                    <SectionLabel>{`Deployments (${items.length})`}</SectionLabel>
                    <div className="mt-3">
                        {!namespaceValid ? (
                            <EmptyState
                                icon={<PackageOpen className="h-6 w-6" />}
                                title="Choose a namespace"
                                message="Enter the existing namespace whose llmd inventories you want to inspect."
                            />
                        ) : items.length === 0 ? (
                            <EmptyState
                                icon={<PackageOpen className="h-6 w-6" />}
                                title="No deployments yet"
                                message="No llmd inventory ConfigMaps were found in this namespace."
                            />
                        ) : (
                            <DeploymentTable
                                items={items}
                                mutatingId={submitting === 'teardown' ? deleteTarget?.execution_id : ''}
                                clusterNameById={{ [LOCAL_CONTEXT_ID]: 'Current context' }}
                                podsLoader={null}
                                onDelete={(item) => { setError(''); setDeleteTarget(item); }}
                            />
                        )}
                    </div>
                </div>
            </section>

            {deleteTarget && (
                <DeleteConfirmation
                    title="Teardown llm-d stack"
                    description={`Remove the inventoried resources for ${deleteTarget.name} from ${deleteTarget.namespace}? The namespace, PVCs, Secrets, and unrecorded objects will remain.`}
                    confirmLabel="Teardown"
                    pending={submitting === 'teardown'}
                    error={error}
                    onDecision={confirmTeardown}
                />
            )}
        </ModulePage>
    );
}

export default NamespaceDeploymentView;
