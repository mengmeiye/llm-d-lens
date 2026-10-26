function optionsFor(entries, valueKey, labelKey) {
    const labels = new Map();
    entries.forEach((entry) => {
        const value = entry?.[valueKey];
        if (!value || labels.has(value)) return;
        labels.set(value, entry?.[labelKey] || value);
    });
    return [...labels.entries()]
        .map(([value, label]) => ({ value, label }))
        .sort((left, right) => left.label.localeCompare(right.label));
}

export function deploymentCatalogFacets(catalog, selection = {}) {
    const supported = (Array.isArray(catalog) ? catalog : []).filter((entry) => entry?.supported);
    const byGuide = selection.guide
        ? supported.filter((entry) => entry.guide === selection.guide)
        : [];
    const byAccelerator = selection.accelerator
        ? byGuide.filter((entry) => entry.accelerator === selection.accelerator)
        : [];
    const byEngine = selection.engine
        ? byAccelerator.filter((entry) => entry.engine === selection.engine)
        : [];
    return {
        architectures: optionsFor(supported, 'guide', 'guide_label'),
        accelerators: optionsFor(byGuide, 'accelerator', 'accelerator_label'),
        engines: optionsFor(byAccelerator, 'engine', 'engine_label'),
        profiles: optionsFor(byEngine, 'overlay', 'variant_label'),
    };
}

export function recommendedAccelerator(options, capabilities) {
    const detected = new Set(capabilities?.accelerators || []);
    const matches = options.filter((option) => detected.has(option.value));
    return matches.length === 1 ? matches[0].value : '';
}

export function recommendedDeploymentProfile(options, capabilities) {
    const recommendation = capabilities?.recommendedProfile;
    if (!recommendation) return '';
    const matches = options.filter(
        (option) => option.value === recommendation || option.value.endsWith(`/${recommendation}`)
    );
    return matches.length === 1 ? matches[0].value : '';
}

export function presentedDeploymentProfiles(options, capabilities) {
    if (capabilities?.platform !== 'openshift') return options;
    return options.map((option) => {
        if (option.value !== 'base' && !option.value.endsWith('/base')) return option;
        const separator = option.label.lastIndexOf(' · ');
        const prefix = separator >= 0 ? option.label.slice(0, separator + 3) : '';
        return { ...option, label: `${prefix}OpenShift (Kubernetes base overlay)` };
    });
}

export function onlyOption(options) {
    return options.length === 1 ? options[0].value : '';
}
