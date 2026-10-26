import assert from 'node:assert/strict';
import test from 'node:test';

import {
    deployNamespaceDeployment,
    getNamespaceClusterCapabilities,
    getNamespaceGuideCatalog,
    getNamespaceDeploymentStatus,
    importNamespaceKubeconfig,
    listNamespaceDeployments,
    removeNamespaceKubeconfigConnection,
    selectNamespaceKubeconfigContext,
    teardownNamespaceDeployment,
} from './namespaceDeploymentBackend.js';

test('namespace deployment client preserves namespace and shared request fields', async (t) => {
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        calls.push({ url, options });
        if (url.includes('/catalog')) return Response.json({ items: [{ guide: 'optimized-baseline' }] });
        if (url.includes('/deployments?')) return Response.json({ items: [{ name: 'demo' }] });
        return Response.json({ status: options.method === 'DELETE' ? 'removed' : 'installed' });
    });

    assert.deepEqual(await getNamespaceGuideCatalog(), [{ guide: 'optimized-baseline' }]);
    assert.deepEqual(await listNamespaceDeployments('team/a'), [{ name: 'demo' }]);
    await deployNamespaceDeployment({
        namespace: 'team-a',
        name: 'demo',
        guide: 'optimized-baseline',
        overlay: 'path',
        model: 'Qwen/Qwen3-32B',
    });
    await getNamespaceDeploymentStatus('team/a', 'demo name');
    await teardownNamespaceDeployment('team/a', 'demo name');

    assert.equal(calls[1].url, '/api/local/v1/deployments?namespace=team%2Fa');
    assert.deepEqual(JSON.parse(calls[2].options.body), {
        namespace: 'team-a', name: 'demo', guide: 'optimized-baseline', overlay: 'path',
        model: 'Qwen/Qwen3-32B',
    });
    assert.equal(calls[3].url, '/api/local/v1/deployments/demo%20name?namespace=team%2Fa');
    assert.equal(calls[3].options.method, undefined);
    assert.equal(calls[4].url, '/api/local/v1/deployments/demo%20name?namespace=team%2Fa');
    assert.equal(calls[4].options.method, 'DELETE');
});

test('namespace deployment client imports, selects, and removes local connections', async (t) => {
    const calls = [];
    t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
        calls.push({ url, options });
        return Response.json({ source: 'uploaded', context: 'team-a', contexts: ['team-a'] });
    });

    const file = new File(['kind: Config\n'], 'config');
    await importNamespaceKubeconfig(file);
    await selectNamespaceKubeconfigContext('team-a', 'uploaded-1');
    await removeNamespaceKubeconfigConnection('uploaded/1');

    assert.equal(calls[0].url, '/api/local/v1/connection');
    assert.equal(calls[0].options.method, 'POST');
    assert.equal(calls[0].options.body.get('file'), file);
    assert.equal(calls[0].options.headers.has('Content-Type'), false);
    assert.equal(calls[1].url, '/api/local/v1/connection/context');
    assert.deepEqual(JSON.parse(calls[1].options.body), { context: 'team-a', connectionId: 'uploaded-1' });
    assert.equal(calls[2].url, '/api/local/v1/connection/uploaded%2F1');
    assert.equal(calls[2].options.method, 'DELETE');
});

test('namespace deployment client reads normalized cluster capabilities', async (t) => {
    let requestedUrl = '';
    t.mock.method(globalThis, 'fetch', async (url) => {
        requestedUrl = url;
        return Response.json({
            accelerators: ['gpu'],
            acceleratorStatus: 'detected',
            platform: 'gke',
            recommendedProfile: 'gke',
        });
    });

    const result = await getNamespaceClusterCapabilities();

    assert.equal(requestedUrl, '/api/local/v1/cluster-capabilities');
    assert.deepEqual(result.accelerators, ['gpu']);
    assert.equal(result.recommendedProfile, 'gke');
});
