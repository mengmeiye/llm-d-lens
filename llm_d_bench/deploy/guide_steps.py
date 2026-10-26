"""Follow an llm-d Guide's machine-readable deploy steps without running shell.

Upstream ``guide.yaml`` files are the source of truth for each Guide's commands
(see the Guide template in the llm-d repository). This module resolves the
standalone router install and the ``kubectl apply -n ${NAMESPACE}`` steps for
one model-server overlay, so a namespace operator gets the same router values
and namespaced manifests the Guide applies. It never executes the commands.

Cluster-wide prerequisites (CRDs, operators, namespaces) and user-supplied
Secrets are left to their owners. Any other step that would change the cluster
in a way this reader cannot reproduce makes the overlay unsupported instead of
being skipped silently.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_BARE_VARIABLE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")
_ENV_DEFAULT = re.compile(r"export ([A-Za-z_][A-Za-z0-9_]*)=\$\{\1:-([^}]*)\}")
_SEPARATORS = {"|", "||", "&&", ";", "&"}
_MUTATING_VERBS = {
    "annotate",
    "apply",
    "create",
    "delete",
    "edit",
    "install",
    "label",
    "patch",
    "replace",
    "scale",
    "set",
    "uninstall",
    "upgrade",
}
_CREATE_NAMESPACE = ["kubectl", "create", "namespace"]
_CREATE_SECRET = ["kubectl", "create", "secret"]
_NAMESPACE = "${NAMESPACE}"
_FIXED_VARIABLES = {"REPO_ROOT", "GUIDE_NAME", "ACCELERATOR_TYPE", "MODEL_SERVER"}


class GuideStepsError(ValueError):
    """The Guide's steps cannot be followed by a namespace-scoped deployer."""


@dataclass(frozen=True)
class GuideManifest:
    """One namespaced manifest the Guide applies: a Kustomize directory or a YAML file."""

    kustomize: bool
    path: Path


@dataclass(frozen=True)
class GuideSteps:
    router_chart: str
    router_chart_version: str
    router_values: tuple[Path, ...]
    manifests: tuple[GuideManifest, ...]


def load_guide_document(guide_root: Path) -> dict[str, Any]:
    path = guide_root / "guide.yaml"
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return payload if isinstance(payload, dict) else {}


def declares_modelserver_steps(document: Mapping[str, Any]) -> bool:
    """Whether ``deploy`` names model-server overlays, making it the authoritative step list."""

    for _section, step in _steps(document.get("deploy")):
        for words in _commands(step):
            target = _simple_apply(words)
            if target and target[0] and "/modelserver" in target[1]:
                return True
    return False


def router_chart_coordinates(source_root: Path, defaults: Mapping[str, str]) -> tuple[str, str]:
    """The standalone router chart and version declared by the source's ``guides/env.sh``."""

    environment = _Environment(source_root, {}, defaults)
    return environment.expand("${ROUTER_STANDALONE_CHART}"), environment.expand("${ROUTER_CHART_VERSION}")


def resolve_guide_steps(
    source_root: Path,
    document: Mapping[str, Any],
    *,
    accelerator: str,
    engine: str,
    overlay: Path,
    defaults: Mapping[str, str],
) -> GuideSteps:
    """Resolve the router install and namespaced manifests for one model-server overlay.

    ``defaults`` supplies values for variables the source does not declare,
    normally the router chart coordinates pinned by Lens.
    """

    environment = _Environment(source_root, document, defaults)
    environment.set("ACCELERATOR_TYPE", accelerator)
    environment.set("MODEL_SERVER", engine)
    selected_section, selected_step, captures = _modelserver_step(document, environment, overlay)
    for name, value in captures.items():
        environment.set(name, value)

    manifests: list[GuideManifest] = []
    router: tuple[str, str, tuple[Path, ...]] | None = None
    for group in ("prerequisites", "deploy"):
        for section, step in _steps(document.get(group)):
            if group == "deploy" and section == "gateway":
                continue  # Gateway Mode needs a cluster Gateway; this deployer uses Standalone Mode.
            if group == "deploy" and section == selected_section and step is not selected_step:
                continue  # Another model-server alternative of the same Guide.
            if not environment.included(step):
                continue
            for words in _commands(step):
                if words[0] == "export":
                    environment.export(words[1:])
                elif group == "deploy" and section == "standalone" and words[0] == "helm":
                    router = _router_install(words, environment, source_root)
                elif (target := _simple_apply(words)) is not None:
                    manifests.append(_manifest(target, environment, source_root, f"{group}.{section}"))
                elif _mutates(words):
                    if group == "prerequisites" and not _targets_namespace(words):
                        continue  # A cluster-wide prerequisite that an administrator installs.
                    raise GuideStepsError(
                        f"guide.yaml step {group}.{section} changes the cluster with a command "
                        "this deployer cannot follow"
                    )
    if router is None:
        raise GuideStepsError("guide.yaml declares no standalone router install")
    chart, version, values = router
    return GuideSteps(chart, version, values, tuple(manifests))


class _Environment:
    def __init__(self, source_root: Path, document: Mapping[str, Any], defaults: Mapping[str, str]) -> None:
        self._values: dict[str, str] = dict(defaults)
        env_script = source_root / "guides" / "env.sh"
        if env_script.is_file():
            for line in env_script.read_text(encoding="utf-8").splitlines():
                if match := _ENV_DEFAULT.fullmatch(line.strip()):
                    self._values[match.group(1)] = match.group(2)
        env = document.get("env") if isinstance(document.get("env"), Mapping) else {}
        static = env.get("static") if isinstance(env.get("static"), Mapping) else {}
        self.choices: dict[str, list[str]] = {}
        for name, spec in static.items():
            value = spec.get("default") if isinstance(spec, Mapping) else spec
            if value is not None:
                self._values[str(name)] = str(value)
            if isinstance(spec, Mapping) and isinstance(spec.get("values"), list):
                self.choices[str(name)] = [str(item) for item in spec["values"]]
        self._values["REPO_ROOT"] = str(source_root)
        # Monitoring needs a cluster monitoring stack, which a namespace operator cannot install.
        self._values["MONITORING"] = "false"

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def lookup(self, name: str) -> str:
        try:
            return self.expand(f"${{{name}}}")
        except GuideStepsError:
            return ""

    def expand(self, text: str, depth: int = 0) -> str:
        if depth > 10:
            raise GuideStepsError("guide.yaml variables refer to each other in a loop")

        def replace(match: re.Match[str]) -> str:
            name, fallback = match.group(1), match.group(2)
            if name in self._values:
                return self.expand(self._values[name], depth + 1)
            if fallback is not None:
                return self.expand(fallback, depth + 1)
            raise GuideStepsError(f"guide.yaml variable {name} has no value")

        return _VARIABLE.sub(replace, text)

    def export(self, assignments: list[str]) -> None:
        for assignment in assignments:
            name, separator, value = assignment.partition("=")
            if not separator:
                continue
            try:
                self._values[name] = self.expand(value)
            except GuideStepsError:
                self._values[name] = value  # Fails later only if a followed step needs it.

    def included(self, step: Mapping[str, Any]) -> bool:
        when = step.get("when")
        if not isinstance(when, Mapping):
            return True
        return all(
            self.lookup(str(name)) in [str(item) for item in allowed]
            for name, allowed in when.items()
            if isinstance(allowed, list)
        )

    def pattern(self, template: str) -> re.Pattern[str] | None:
        """A regex for a path template: declared choices become groups, other variables literals."""

        parts: list[str] = []
        seen: set[str] = set()
        position = 0
        for match in _VARIABLE.finditer(template):
            parts.append(re.escape(template[position : match.start()]))
            name = match.group(1)
            if name in self.choices and name not in _FIXED_VARIABLES:
                if name in seen:
                    parts.append(f"(?P={name})")
                else:
                    options = sorted(self.choices[name], key=len, reverse=True)
                    parts.append(f"(?P<{name}>{'|'.join(re.escape(option) for option in options)})")
                    seen.add(name)
            else:
                try:
                    parts.append(re.escape(self.expand(match.group(0))))
                except GuideStepsError:
                    return None
            position = match.end()
        parts.append(re.escape(template[position:]))
        return re.compile("".join(parts))


def _steps(node: Any, section: str | None = None) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if isinstance(node, Mapping) and "run" in node:
        if section is not None:
            yield section, node
        return
    if isinstance(node, list):
        for step in node:
            if isinstance(step, Mapping) and "run" in step and section is not None:
                yield section, step
        return
    if isinstance(node, Mapping):
        for key, child in node.items():
            yield from _steps(child, section or str(key))


def _commands(step: Mapping[str, Any]) -> Iterator[list[str]]:
    """Each shell command of a step as words, with pipelines and chains kept together."""

    text = str(step.get("run") or "").replace("\\\n", " ")
    for line in text.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            words = list(lexer)
        except ValueError as error:
            if "kubectl" in line or "helm" in line:
                raise GuideStepsError(f"guide.yaml command cannot be parsed: {line.strip()[:80]}") from error
            continue
        if words:
            yield words


def _segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(word)
    return [segment for segment in segments if segment]


def _targets_namespace(words: list[str]) -> bool:
    return any(
        (word in ("-n", "--namespace") and index + 1 < len(words) and words[index + 1] == _NAMESPACE)
        or word == f"--namespace={_NAMESPACE}"
        for index, word in enumerate(words)
    )


def _mutates(words: list[str]) -> bool:
    """Whether a kubectl or Helm command changes the cluster, apart from creating the namespace or Secrets."""

    segments = _segments(words)
    # The namespace owner creates the namespace and the user supplies credentials.
    if any(segment[:3] in (_CREATE_NAMESPACE, _CREATE_SECRET) for segment in segments):
        return False
    for segment in segments:
        if segment[0] in ("kubectl", "helm"):
            verb = next((word for word in segment[1:] if not word.startswith("-")), "")
            if verb in _MUTATING_VERBS:
                return True
    return False


def _simple_apply(words: list[str]) -> tuple[bool, str] | None:
    """``kubectl apply -n ${NAMESPACE} -k|-f PATH`` as ``(is_kustomize, raw_path)``."""

    if len(_segments(words)) != 1 or words[:2] != ["kubectl", "apply"] or not _targets_namespace(words):
        return None
    remaining = list(words[2:])
    target: tuple[bool, str] | None = None
    while remaining:
        word = remaining.pop(0)
        if word in ("-n", "--namespace") and remaining:
            remaining.pop(0)
        elif word.startswith("--namespace="):
            continue
        elif word in ("-k", "--kustomize", "-f", "--filename") and remaining and target is None:
            target = (word in ("-k", "--kustomize"), remaining.pop(0))
        else:
            return None
    return target


def _manifest(target: tuple[bool, str], environment: _Environment, source_root: Path, step: str) -> GuideManifest:
    kustomize, raw_path = target
    expanded = environment.expand(raw_path)
    if "://" in expanded:
        raise GuideStepsError(f"guide.yaml step {step} applies a remote manifest")
    path = Path(expanded).resolve()
    if not path.is_relative_to(source_root.resolve()):
        raise GuideStepsError(f"guide.yaml step {step} applies a manifest outside the source checkout")
    if kustomize and not any((path / name).is_file() for name in ("kustomization.yaml", "kustomization.yml")):
        raise GuideStepsError(f"guide.yaml step {step} applies a missing Kustomize directory")
    if not kustomize and not path.is_file():
        raise GuideStepsError(f"guide.yaml step {step} applies a missing manifest file")
    return GuideManifest(kustomize=kustomize, path=path)


def _modelserver_step(
    document: Mapping[str, Any],
    environment: _Environment,
    overlay: Path,
) -> tuple[str, Mapping[str, Any], dict[str, str]]:
    target = str(overlay.resolve())
    for section, step in _steps(document.get("deploy")):
        if section == "gateway":
            continue
        for words in _commands(step):
            apply = _simple_apply(words)
            if apply is None or not apply[0] or "/modelserver" not in apply[1]:
                continue
            pattern = environment.pattern(apply[1].rstrip("/"))
            match = pattern.fullmatch(target) if pattern else None
            if match is None:
                continue
            captures = {name: value for name, value in match.groupdict().items() if value is not None}
            previous = {name: environment.lookup(name) for name in captures}
            for name, value in captures.items():
                environment.set(name, value)
            selected = environment.included(step)
            for name, value in previous.items():
                environment.set(name, value)
            if selected:
                return section, step, captures
    raise GuideStepsError("overlay is not a model-server entry point declared in guide.yaml")


def _router_install(
    words: list[str],
    environment: _Environment,
    source_root: Path,
) -> tuple[str, str, tuple[Path, ...]]:
    if len(_segments(words)) != 1:
        raise GuideStepsError("guide.yaml router install needs shell execution")
    if words[1:2] == ["install"]:
        remaining = list(words[2:])
    elif words[1:3] == ["upgrade", "--install"]:
        remaining = list(words[3:])
    else:
        raise GuideStepsError("guide.yaml router step is not a Helm install")
    if len(remaining) < 2:
        raise GuideStepsError("guide.yaml router install names no chart")
    chart = environment.expand(remaining[1])
    remaining = remaining[2:]
    version = ""
    values: list[Path] = []
    while remaining:
        word = remaining.pop(0)
        if _BARE_VARIABLE.fullmatch(word):
            remaining[:0] = shlex.split(environment.expand(word))
        elif word in ("-f", "--values") and remaining:
            path = Path(environment.expand(remaining.pop(0))).resolve()
            if not path.is_relative_to(source_root.resolve()) or not path.is_file():
                raise GuideStepsError("guide.yaml router values are unavailable in the source checkout")
            values.append(path)
        elif word in ("-n", "--namespace") and remaining:
            remaining.pop(0)
        elif word == "--version" and remaining:
            version = environment.expand(remaining.pop(0))
        else:
            raise GuideStepsError(f"guide.yaml router install uses unsupported option {word}")
    if not values:
        raise GuideStepsError("guide.yaml router install declares no values files")
    if not version:
        raise GuideStepsError("guide.yaml router install declares no chart version")
    return chart, version, tuple(values)
