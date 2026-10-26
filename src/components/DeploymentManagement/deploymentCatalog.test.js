import assert from 'node:assert/strict';
import test from 'node:test';

import {
    deploymentCatalogFacets,
    onlyOption,
    presentedDeploymentProfiles,
    recommendedAccelerator,
    recommendedDeploymentProfile,
} from './deploymentCatalog.js';

const entries = [
    {
        guide: 'optimized-baseline',
        guide_label: 'Optimized baseline',
        accelerator: 'gpu',
        accelerator_label: 'NVIDIA GPU',
        engine: 'vllm',
        engine_label: 'vLLM',
        overlay: 'guides/optimized-baseline/modelserver/gpu/vllm/base',
        variant_label: 'Kubernetes (base)',
        supported: true,
    },
    {
        guide: 'optimized-baseline',
        guide_label: 'Optimized baseline',
        accelerator: 'gpu',
        accelerator_label: 'NVIDIA GPU',
        engine: 'trtllm',
        engine_label: 'TensorRT-LLM',
        overlay: 'guides/optimized-baseline/modelserver/gpu/trtllm/base',
        variant_label: 'Kubernetes (base)',
        supported: true,
    },
    {
        guide: 'optimized-baseline',
        guide_label: 'Optimized baseline',
        accelerator: 'gpu',
        accelerator_label: 'NVIDIA GPU',
        engine: 'vllm',
        engine_label: 'vLLM',
        overlay: 'guides/optimized-baseline/modelserver/gpu/vllm/gke',
        variant_label: 'GKE',
        supported: true,
    },
    {
        guide: 'optimized-baseline',
        guide_label: 'Optimized baseline',
        accelerator: 'xpu',
        accelerator_label: 'Intel XPU',
        engine: 'vllm',
        engine_label: 'vLLM',
        overlay: 'guides/optimized-baseline/modelserver/xpu/vllm/base',
        variant_label: 'Kubernetes (base)',
        supported: true,
    },
    {
        guide: 'pd-disaggregation',
        guide_label: 'P/D disaggregation',
        accelerator: 'gpu',
        accelerator_label: 'NVIDIA GPU',
        engine: 'vllm',
        engine_label: 'vLLM',
        overlay: 'guides/pd-disaggregation/modelserver/gpu/vllm/base',
        variant_label: 'Kubernetes (base)',
        supported: true,
    },
    {
        guide: 'recipes',
        guide_label: 'Recipes',
        accelerator: 'unknown',
        engine: 'unknown',
        overlay: 'guides/recipes/modelserver/common',
        supported: false,
    },
];

test('catalog facets show only supported choices and cascade by upstream coordinates', () => {
    const facets = deploymentCatalogFacets(entries, {
        guide: 'optimized-baseline', accelerator: 'gpu', engine: 'vllm',
    });

    assert.deepEqual(facets.architectures, [
        { value: 'optimized-baseline', label: 'Optimized baseline' },
        { value: 'pd-disaggregation', label: 'P/D disaggregation' },
    ]);
    assert.deepEqual(facets.accelerators, [
        { value: 'xpu', label: 'Intel XPU' },
        { value: 'gpu', label: 'NVIDIA GPU' },
    ]);
    assert.deepEqual(facets.engines, [
        { value: 'trtllm', label: 'TensorRT-LLM' },
        { value: 'vllm', label: 'vLLM' },
    ]);
    assert.deepEqual(facets.profiles, [
        { value: 'guides/optimized-baseline/modelserver/gpu/vllm/gke', label: 'GKE' },
        {
            value: 'guides/optimized-baseline/modelserver/gpu/vllm/base',
            label: 'Kubernetes (base)',
        },
    ]);
});

test('accelerator and platform recommendations apply only when one available choice matches', () => {
    const facets = deploymentCatalogFacets(entries, {
        guide: 'optimized-baseline', accelerator: 'gpu', engine: 'vllm',
    });

    assert.equal(recommendedAccelerator(facets.accelerators, { accelerators: ['gpu'] }), 'gpu');
    assert.equal(recommendedAccelerator(facets.accelerators, { accelerators: ['gpu', 'xpu'] }), '');
    assert.equal(recommendedDeploymentProfile(facets.profiles, { recommendedProfile: 'gke' }), entries[2].overlay);
    assert.equal(recommendedDeploymentProfile(facets.profiles, { recommendedProfile: 'aws' }), '');
    assert.equal(onlyOption(facets.engines), '');
});

test('a platform recommendation stays manual when several nested profiles match', () => {
    const options = [
        { value: 'native/cpu/gke', label: 'Native CPU · GKE' },
        { value: 'lmcache-connector/cpu/gke', label: 'LMCache CPU · GKE' },
    ];

    assert.equal(recommendedDeploymentProfile(options, { recommendedProfile: 'gke' }), '');
});

test('TensorRT-LLM appears only for combinations declared by the Guide catalog', () => {
    const gpu = deploymentCatalogFacets(entries, {
        guide: 'optimized-baseline', accelerator: 'gpu', engine: 'trtllm',
    });
    const xpu = deploymentCatalogFacets(entries, {
        guide: 'optimized-baseline', accelerator: 'xpu', engine: 'trtllm',
    });
    const otherArchitecture = deploymentCatalogFacets(entries, {
        guide: 'pd-disaggregation', accelerator: 'gpu', engine: 'trtllm',
    });

    assert.deepEqual(gpu.profiles, [{
        value: 'guides/optimized-baseline/modelserver/gpu/trtllm/base',
        label: 'Kubernetes (base)',
    }]);
    assert.deepEqual(xpu.engines, [{ value: 'vllm', label: 'vLLM' }]);
    assert.deepEqual(otherArchitecture.engines, [{ value: 'vllm', label: 'vLLM' }]);
});

test('OpenShift presents existing base profiles without creating another overlay', () => {
    const options = [
        { value: 'guides/example/modelserver/gpu/vllm/base', label: 'Kubernetes (base)' },
        { value: 'guides/example/modelserver/gpu/vllm/native/cpu/base', label: 'Native CPU · Kubernetes (base)' },
        { value: 'guides/example/modelserver/gpu/vllm/gke', label: 'GKE' },
    ];

    assert.deepEqual(presentedDeploymentProfiles(options, { platform: 'openshift' }), [
        {
            value: 'guides/example/modelserver/gpu/vllm/base',
            label: 'OpenShift (Kubernetes base overlay)',
        },
        {
            value: 'guides/example/modelserver/gpu/vllm/native/cpu/base',
            label: 'Native CPU · OpenShift (Kubernetes base overlay)',
        },
        { value: 'guides/example/modelserver/gpu/vllm/gke', label: 'GKE' },
    ]);
    assert.deepEqual(presentedDeploymentProfiles(options, { platform: 'kubernetes' }), options);
});
