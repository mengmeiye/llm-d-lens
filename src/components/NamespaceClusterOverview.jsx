import { useCallback, useEffect, useMemo, useState } from 'react';
import { Network, Rocket, Server, Trash2 } from 'lucide-react';

import { errorMessage } from '../utils/errorMessage';
import { cn } from '../utils/cn';
import {
    getNamespaceDeploymentInfo,
    importNamespaceKubeconfig,
    removeNamespaceKubeconfigConnection,
    selectNamespaceKubeconfigContext,
} from './DeploymentManagement/namespaceDeploymentBackend.js';
import { KubeconfigFilePicker } from './shared/KubeconfigFilePicker';
import { LocalModeHeaderActions } from './shared/LocalModeHeaderActions';
import {
    Badge,
    Button,
    EmptyState,
    FormError,
    LoadingState,
    ModuleHeader,
    ModulePage,
    Panel,
} from './ui';

function connectionEntries(connection) {
    const connections = Array.isArray(connection?.connections) && connection.connections.length > 0
        ? connection.connections
        : connection
            ? [{
                id: connection.activeConnectionId || 'active',
                label: connection.source === 'startup' ? 'Startup kubeconfig' : 'Current kubectl context',
                source: connection.source,
                context: connection.context,
                contexts: connection.contexts,
                ephemeral: connection.ephemeral,
                active: true,
            }]
            : [];

    return connections.flatMap((item) => {
        const contexts = Array.isArray(item.contexts) && item.contexts.length > 0
            ? item.contexts
            : [item.context || null];
        return contexts.map((context) => ({
            connectionId: item.id,
            connectionLabel: item.label,
            source: item.source,
            ephemeral: item.ephemeral,
            context,
            active: item.id === connection?.activeConnectionId && context === connection?.context,
        }));
    });
}

function contextLabel(entry) {
    return entry.context || 'Current kubectl context';
}

export function NamespaceClusterOverview({ onNavigate, onToggleMobileNav } = {}) {
    const [info, setInfo] = useState(null);
    const [kubeconfigFile, setKubeconfigFile] = useState(null);
    const [loading, setLoading] = useState(true);
    const [pending, setPending] = useState('');
    const [error, setError] = useState('');

    const entries = useMemo(() => connectionEntries(info?.connection), [info]);

    const load = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            setInfo(await getNamespaceDeploymentInfo());
        } catch (loadError) {
            setError(errorMessage(loadError));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const importKubeconfig = async () => {
        if (!kubeconfigFile || pending) return;
        setPending('import');
        setError('');
        try {
            const connection = await importNamespaceKubeconfig(kubeconfigFile);
            setInfo((current) => ({ ...current, connection }));
            setKubeconfigFile(null);
        } catch (importError) {
            setError(errorMessage(importError));
        } finally {
            setPending('');
        }
    };

    const selectEntry = async (entry) => {
        if (entry.active) return true;
        const operation = `select:${entry.connectionId}:${entry.context || ''}`;
        setPending(operation);
        setError('');
        try {
            const connection = await selectNamespaceKubeconfigContext(entry.context, entry.connectionId);
            setInfo((current) => ({ ...current, connection }));
            return true;
        } catch (selectError) {
            setError(errorMessage(selectError));
            return false;
        } finally {
            setPending('');
        }
    };

    const deployTo = async (entry) => {
        if (await selectEntry(entry)) onNavigate?.('optimization-deployments');
    };

    const removeImport = async (connectionId) => {
        if (pending) return;
        setPending(`remove:${connectionId}`);
        setError('');
        try {
            const connection = await removeNamespaceKubeconfigConnection(connectionId);
            setInfo((current) => ({ ...current, connection }));
        } catch (removeError) {
            setError(errorMessage(removeError));
        } finally {
            setPending('');
        }
    };

    if (loading && !info) return <LoadingState fullPage label="Loading cluster connections" />;

    return (
        <ModulePage contentClassName="flex flex-col gap-4">
            <ModuleHeader
                icon={Network}
                title="Clusters"
                description="Connect local Lens to Kubernetes clusters with kubeconfig files, then choose where to deploy."
                onToggleMobileNav={onToggleMobileNav}
                actions={(
                    <LocalModeHeaderActions
                        databaseFree={info?.database === false}
                        onRefresh={load}
                        refreshing={loading}
                        disabled={Boolean(pending)}
                        refreshLabel="Refresh cluster connections"
                    />
                )}
            />

            <Panel title="Import kubeconfig">
                <p className="mb-4 text-sm leading-6 text-theme-muted">
                    Import one or more kubeconfig files for this local session. Lens keeps each upload in a restricted temporary file, never stores it in a database, and removes it when the UI exits.
                </p>
                <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
                    <div className="min-w-0 flex-1">
                        <KubeconfigFilePicker
                            inputId="local-cluster-kubeconfig"
                            file={kubeconfigFile}
                            onFileChange={(file) => { setKubeconfigFile(file); setError(''); }}
                            disabled={Boolean(pending)}
                        />
                    </div>
                    <Button
                        variant="sky"
                        size="md"
                        onClick={importKubeconfig}
                        isLoading={pending === 'import'}
                        disabled={!kubeconfigFile || Boolean(pending)}
                    >
                        Import kubeconfig
                    </Button>
                </div>
                <FormError message={error} className="mt-3" />
            </Panel>

            <Panel
                title={`Cluster contexts (${entries.length})`}
                actions={<Badge tone="neutral">Session only</Badge>}
            >
                {entries.length === 0 ? (
                    <EmptyState
                        icon={<Server className="h-6 w-6" />}
                        title="No cluster connections"
                        message="Import a kubeconfig to make its contexts available to Deployments."
                    />
                ) : (
                    <div className="grid gap-3 lg:grid-cols-2">
                        {entries.map((entry) => {
                            const key = `${entry.connectionId}:${entry.context || 'ambient'}`;
                            const selectPending = pending === `select:${entry.connectionId}:${entry.context || ''}`;
                            const removePending = pending === `remove:${entry.connectionId}`;
                            return (
                                <article
                                    key={key}
                                    className={cn(
                                        'rounded-xl border p-4 transition-colors',
                                        entry.active
                                            ? 'border-sky-500/50 bg-sky-500/10'
                                            : 'border-theme-border bg-theme-bg'
                                    )}
                                >
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="flex min-w-0 items-start gap-3">
                                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-theme-border bg-theme-card text-sky-400">
                                                <Server size={17} aria-hidden="true" />
                                            </div>
                                            <div className="min-w-0">
                                                <h3 className="truncate text-sm font-semibold text-theme-text">{contextLabel(entry)}</h3>
                                                <p className="mt-1 truncate text-xs text-theme-muted">{entry.connectionLabel}</p>
                                            </div>
                                        </div>
                                        <Badge tone={entry.active ? 'success' : 'neutral'}>
                                            {entry.active ? 'Selected' : 'Available'}
                                        </Badge>
                                    </div>
                                    <div className="mt-3 flex flex-wrap gap-2">
                                        <Badge tone="neutral" size="md">
                                            {entry.source === 'uploaded'
                                                ? 'Imported kubeconfig'
                                                : entry.source === 'startup'
                                                    ? 'Startup kubeconfig'
                                                    : 'Ambient context'}
                                        </Badge>
                                    </div>
                                    <div className="mt-4 flex flex-wrap gap-2">
                                        {!entry.active && (
                                            <Button
                                                variant="secondary"
                                                onClick={() => selectEntry(entry)}
                                                isLoading={selectPending}
                                                disabled={Boolean(pending)}
                                            >
                                                Select cluster
                                            </Button>
                                        )}
                                        <Button
                                            variant="sky"
                                            onClick={() => deployTo(entry)}
                                            isLoading={selectPending}
                                            disabled={Boolean(pending)}
                                        >
                                            <Rocket size={14} aria-hidden="true" />
                                            Deploy to this cluster
                                        </Button>
                                        {entry.ephemeral && (
                                            <Button
                                                variant="dangerOutline"
                                                onClick={() => removeImport(entry.connectionId)}
                                                isLoading={removePending}
                                                disabled={Boolean(pending)}
                                            >
                                                <Trash2 size={14} aria-hidden="true" />
                                                Remove import
                                            </Button>
                                        )}
                                    </div>
                                </article>
                            );
                        })}
                    </div>
                )}
            </Panel>
        </ModulePage>
    );
}

export default NamespaceClusterOverview;
