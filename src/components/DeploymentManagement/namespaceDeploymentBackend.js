import { postJson, requestJson } from '../../api/httpClient.js';

const ROOT = '/api/local/v1';

export function getNamespaceDeploymentInfo(options = {}) {
    return requestJson(`${ROOT}/info`, options);
}

export function importNamespaceKubeconfig(file, options = {}) {
    const body = new FormData();
    body.append('file', file);
    return requestJson(`${ROOT}/connection`, { ...options, method: 'POST', body });
}

export function selectNamespaceKubeconfigContext(context, connectionId = null, options = {}) {
    return postJson(`${ROOT}/connection/context`, { context, connectionId }, options);
}

export function removeNamespaceKubeconfigConnection(connectionId, options = {}) {
    return requestJson(
        `${ROOT}/connection/${encodeURIComponent(connectionId)}`,
        { ...options, method: 'DELETE' }
    );
}

export async function getNamespaceGuideCatalog(options = {}) {
    const response = await requestJson(`${ROOT}/catalog`, options);
    return Array.isArray(response?.items) ? response.items : [];
}

export function getNamespaceClusterCapabilities(options = {}) {
    return requestJson(`${ROOT}/cluster-capabilities`, options);
}

export function renderNamespaceDeployment(request, options = {}) {
    return postJson(`${ROOT}/render`, request, options);
}

export function deployNamespaceDeployment(request, options = {}) {
    return postJson(`${ROOT}/deployments`, request, options);
}

export async function listNamespaceDeployments(namespace, options = {}) {
    const response = await requestJson(`${ROOT}/deployments?namespace=${encodeURIComponent(namespace)}`, options);
    return Array.isArray(response?.items) ? response.items : [];
}

export function getNamespaceDeploymentStatus(namespace, name, options = {}) {
    return requestJson(
        `${ROOT}/deployments/${encodeURIComponent(name)}?namespace=${encodeURIComponent(namespace)}`,
        options
    );
}

export function teardownNamespaceDeployment(namespace, name, options = {}) {
    return requestJson(
        `${ROOT}/deployments/${encodeURIComponent(name)}?namespace=${encodeURIComponent(namespace)}`,
        { ...options, method: 'DELETE' }
    );
}
