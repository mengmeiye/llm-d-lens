"""Pure Kubernetes object-name validation shared by local and server runtimes."""

from __future__ import annotations

import re

_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def validate_namespace(value: str) -> str:
    """Validate a Kubernetes namespace without normalizing caller input."""
    if len(value) > 63 or not _NAMESPACE.fullmatch(value):
        raise ValueError("namespace must be a valid Kubernetes DNS label")
    return value
