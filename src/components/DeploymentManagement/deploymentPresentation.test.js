import assert from 'node:assert/strict';
import test from 'node:test';

import {
    derivePodStatusGroup,
    effectiveStatusGroup,
    effectiveStatusLabel,
    statusGroup,
    summarizeStatuses,
} from './deploymentPresentation.js';

const pod = (overrides = {}) => ({
    name: 'model',
    phase: 'Running',
    ready: true,
    status_reason: 'Running',
    restarts: 0,
    ...overrides,
});

test('a completed (calibration) pod does not mark a ready deployment not-ready', () => {
    const pods = [
        pod(),
        { name: 'calibrate-peak-throughput-9dgr6', phase: 'Succeeded', ready: false, status_reason: 'Completed', restarts: 0 },
        pod({ name: 'epp' }),
    ];

    assert.equal(derivePodStatusGroup(pods), 'Ready');
    assert.equal(effectiveStatusGroup({ status: 'ready' }, pods), 'Ready');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY');
});

test('only terminal pods yield no conclusive pod status', () => {
    assert.equal(derivePodStatusGroup([{ phase: 'Succeeded', ready: false }]), null);
});

test('a running not-ready pod still downgrades a ready deployment', () => {
    const pods = [pod(), pod({ name: 'epp', ready: false })];

    assert.equal(derivePodStatusGroup(pods), 'In progress');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY · POD NOT READY');
});

test('a completed pod does not hide a genuine pod failure', () => {
    const pods = [
        { name: 'calibrate-peak-throughput-9dgr6', phase: 'Succeeded', ready: false },
        pod({ name: 'model', phase: 'Failed', ready: false, status_reason: 'Failed' }),
    ];

    assert.equal(derivePodStatusGroup(pods), 'Failed');
    assert.equal(effectiveStatusLabel({ status: 'ready' }, pods), 'READY · POD ERROR');
});

test('namespace inventory states use the shared deployment summary groups', () => {
    assert.equal(statusGroup('installed'), 'Ready');
    assert.equal(statusGroup('degraded'), 'Failed');
    assert.equal(statusGroup('unreachable'), 'Failed');
    assert.equal(statusGroup('removed'), 'Cleaned');
    assert.deepEqual(
        summarizeStatuses([
            { status: 'installed' },
            { status: 'degraded' },
            { status: 'unreachable' },
        ]),
        { total: 3, Ready: 1, 'In progress': 0, Failed: 2, Cleaned: 0 }
    );
});
