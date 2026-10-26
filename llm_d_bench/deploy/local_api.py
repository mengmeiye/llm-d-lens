"""Loopback-only API composition for the database-free local Lens entrance."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Annotated
from uuid import uuid4

import yaml
from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from llm_d_bench.core.exceptions import DomainError, DomainValidationError
from llm_d_bench.deploy.namespace_deployer import DeploymentCommandError, NamespaceDeployer
from llm_d_bench.utils.paths import prism_temp_root
from llm_d_bench.utils.problems import problem

_MAX_KUBECONFIG_BYTES = 1024 * 1024


class NamespaceDeploymentRequest(BaseModel):
    """Shared request fields for local render and deploy operations."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=47)
    guide: str = Field(min_length=1, max_length=100)
    overlay: str = Field(min_length=1, max_length=500)
    model: str | None = Field(default=None, min_length=3, max_length=96)


class LocalContextRequest(BaseModel):
    """Select one process-local kubeconfig connection and one of its contexts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context: str | None = Field(default=None, max_length=253)
    connection_id: str | None = Field(default=None, alias="connectionId", min_length=1, max_length=80)


@dataclass(frozen=True)
class _KubeconfigMetadata:
    contexts: tuple[str, ...]
    current_context: str | None


@dataclass(frozen=True)
class _LocalConnection:
    identifier: str
    label: str
    source: str
    kubeconfig: str | None
    metadata: _KubeconfigMetadata
    context: str | None


class LocalConnectionManager:
    """Own active and imported local connections without persistent storage."""

    def __init__(self, deployer: NamespaceDeployer) -> None:
        self._template = deployer
        self._deployer = deployer
        self._lock = Lock()
        self._temporary_directories: list[tempfile.TemporaryDirectory[str]] = []
        source = "startup" if deployer.kubeconfig else "ambient"
        metadata = _metadata_from_path(deployer.kubeconfig)
        context = deployer.context or metadata.current_context
        if context and context not in metadata.contexts:
            metadata = _KubeconfigMetadata((*metadata.contexts, context), context)
        self._default_connection_id = source
        self._active_connection_id = source
        self._connections = {
            source: _LocalConnection(
                identifier=source,
                label="Startup kubeconfig" if source == "startup" else "Current kubectl context",
                source=source,
                kubeconfig=deployer.kubeconfig,
                metadata=metadata,
                context=context,
            )
        }

    def current_deployer(self) -> NamespaceDeployer:
        with self._lock:
            return self._deployer

    def info(self) -> dict:
        with self._lock:
            return self._info_unlocked()

    def import_kubeconfig(self, raw: bytes, filename: str | None = None) -> dict:
        metadata = _parse_kubeconfig(raw)
        selected_context = metadata.current_context or metadata.contexts[0]

        scratch = prism_temp_root("local-ui-kubeconfigs")
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        scratch.chmod(0o700)
        temporary = tempfile.TemporaryDirectory(prefix="connection-", dir=scratch)
        directory = Path(temporary.name)
        directory.chmod(0o700)
        kubeconfig = directory / "config"
        descriptor = os.open(kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
        except Exception:
            temporary.cleanup()
            raise

        with self._lock:
            self._temporary_directories.append(temporary)
            identifier = f"uploaded-{uuid4().hex}"
            self._connections[identifier] = _LocalConnection(
                identifier=identifier,
                label=_upload_label(filename, len(self._temporary_directories)),
                source="uploaded",
                kubeconfig=str(kubeconfig),
                metadata=metadata,
                context=selected_context,
            )
            self._active_connection_id = identifier
            self._deployer = self._template.with_connection(kubeconfig, selected_context)
            return self._info_unlocked()

    def select_context(self, context: str | None, connection_id: str | None = None) -> dict:
        clean_context = context.strip() if context else None
        with self._lock:
            identifier = connection_id or self._active_connection_id
            selected = self._connections.get(identifier)
            if selected is None:
                raise DomainValidationError("Local cluster connection was not found")
            if selected.metadata.contexts and clean_context not in selected.metadata.contexts:
                raise DomainValidationError("Context is not present in the selected kubeconfig")
            if not selected.metadata.contexts and clean_context:
                raise DomainValidationError("The ambient connection does not declare selectable contexts")
            selected = replace(selected, context=clean_context)
            self._connections[identifier] = selected
            self._active_connection_id = identifier
            self._deployer = self._template.with_connection(selected.kubeconfig, clean_context)
            return self._info_unlocked()

    def remove_connection(self, connection_id: str) -> dict:
        with self._lock:
            selected = self._connections.get(connection_id)
            if selected is None:
                raise DomainValidationError("Local cluster connection was not found")
            if selected.source != "uploaded":
                raise DomainValidationError("The startup or ambient connection cannot be removed")
            del self._connections[connection_id]
            if self._active_connection_id == connection_id:
                fallback = self._connections[self._default_connection_id]
                self._active_connection_id = fallback.identifier
                self._deployer = self._template.with_connection(fallback.kubeconfig, fallback.context)
            return self._info_unlocked()

    def _info_unlocked(self) -> dict:
        active = self._connections[self._active_connection_id]
        return {
            "source": active.source,
            "context": active.context,
            "contexts": list(active.metadata.contexts),
            "ephemeral": active.source == "uploaded",
            "activeConnectionId": active.identifier,
            "connections": [
                {
                    "id": item.identifier,
                    "label": item.label,
                    "source": item.source,
                    "context": item.context,
                    "contexts": list(item.metadata.contexts),
                    "ephemeral": item.source == "uploaded",
                    "active": item.identifier == active.identifier,
                }
                for item in self._connections.values()
            ],
        }

    def close(self) -> None:
        with self._lock:
            temporary_directories = self._temporary_directories
            self._temporary_directories = []
        for temporary in temporary_directories:
            temporary.cleanup()


def create_local_router(connection: LocalConnectionManager) -> APIRouter:
    router = APIRouter(prefix="/api/local/v1", tags=["local-deployments"])

    @router.get("/info")
    async def info() -> dict:
        deployer = connection.current_deployer()
        return {
            "mode": "local",
            "source": str(deployer.source_root) if deployer.source_root else None,
            "database": False,
            "connection": connection.info(),
        }

    @router.post("/connection")
    async def import_connection(request: Request, file: Annotated[UploadFile, File()]) -> dict:
        _require_same_origin(request)
        raw = await file.read(_MAX_KUBECONFIG_BYTES + 1)
        if len(raw) > _MAX_KUBECONFIG_BYTES:
            raise DomainValidationError("Kubeconfig must not exceed 1 MiB")
        return connection.import_kubeconfig(raw, file.filename)

    @router.post("/connection/context")
    async def select_connection_context(request: LocalContextRequest) -> dict:
        return connection.select_context(request.context, request.connection_id)

    @router.delete("/connection/{connection_id}")
    async def remove_connection(connection_id: str) -> dict:
        return connection.remove_connection(connection_id)

    @router.get("/catalog")
    async def catalog() -> dict:
        deployer = connection.current_deployer()
        return {"items": [item.__dict__ for item in deployer.catalog()]}

    @router.get("/cluster-capabilities")
    async def cluster_capabilities() -> dict:
        deployer = connection.current_deployer()
        return (await deployer.capabilities()).payload()

    @router.post("/render")
    async def render(request: NamespaceDeploymentRequest) -> dict:
        deployer = connection.current_deployer()
        plan = await deployer.render(**request.model_dump(), preflight=True)
        return plan.summary()

    @router.get("/deployments")
    async def list_deployments(namespace: str = Query(min_length=1, max_length=63)) -> dict:
        deployer = connection.current_deployer()
        return {"items": await deployer.list(namespace)}

    @router.get("/deployments/{name}")
    async def deployment_status(name: str, namespace: str = Query(min_length=1, max_length=63)) -> dict:
        deployer = connection.current_deployer()
        return await deployer.status(namespace, name)

    @router.post("/deployments", status_code=201)
    async def deploy(request: NamespaceDeploymentRequest) -> dict:
        deployer = connection.current_deployer()
        inventory = await deployer.deploy(**request.model_dump())
        return {**inventory.payload(), "status": "installed"}

    @router.delete("/deployments/{name}")
    async def teardown(name: str, namespace: str = Query(min_length=1, max_length=63)) -> dict:
        deployer = connection.current_deployer()
        return await deployer.teardown(namespace, name)

    return router


def create_local_app(deployer: NamespaceDeployer, *, web_root: str | Path | None = None) -> FastAPI:
    """Create a small API that imports no auth, database, or cluster registry services."""

    connection = LocalConnectionManager(deployer)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            connection.close()

    app = FastAPI(
        title="Lens local deployer API",
        description="Namespace-scoped llm-d deployment from a local Guide checkout",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.local_connection = connection
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
        www_redirect=False,
    )
    _install_problem_handlers(app)

    @app.exception_handler(DeploymentCommandError)
    async def command_problem(_request, error: DeploymentCommandError) -> JSONResponse:
        return problem(502, "Cluster command failed", str(error), "deployment_command_failed")

    app.include_router(create_local_router(connection))

    assets = Path(web_root).resolve() if web_root else Path(__file__).resolve().parents[2] / "dist"
    index = assets / "index.html"

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def frontend(asset_path: str):
        requested = (assets / asset_path).resolve()
        if asset_path and requested.is_relative_to(assets) and requested.is_file():
            return FileResponse(requested)
        if not index.is_file():
            return problem(
                503,
                "Local UI is not built",
                "Run npm run build in the Lens source checkout, then restart llmd ui.",
                "local_ui_unavailable",
            )
        html = index.read_text(encoding="utf-8").replace(
            "</head>", "<script>window.__LLMD_LOCAL_MODE__ = true;</script></head>"
        )
        return HTMLResponse(html)

    return app


def _metadata_from_path(kubeconfig: str | None) -> _KubeconfigMetadata:
    if not kubeconfig:
        return _KubeconfigMetadata((), None)
    try:
        return _parse_kubeconfig(Path(kubeconfig).read_bytes())
    except (OSError, DomainValidationError):
        return _KubeconfigMetadata((), None)


def _upload_label(filename: str | None, index: int) -> str:
    candidate = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return candidate[:120] or f"Imported kubeconfig {index}"


def _parse_kubeconfig(raw: bytes) -> _KubeconfigMetadata:
    if not raw.strip():
        raise DomainValidationError("A kubeconfig file is required")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DomainValidationError("Kubeconfig must be UTF-8 text") from error
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise DomainValidationError("Uploaded kubeconfig is not valid YAML") from error
    if not isinstance(payload, dict) or payload.get("kind") not in (None, "Config"):
        raise DomainValidationError("Uploaded file is not a valid kubeconfig")
    cluster_entries = payload.get("clusters")
    context_entries = payload.get("contexts")
    if not isinstance(cluster_entries, list) or not isinstance(context_entries, list):
        raise DomainValidationError("Kubeconfig must define at least one context")
    clusters = {
        item.get("name").strip()
        for item in cluster_entries
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name").strip()
    }
    contexts = tuple(
        dict.fromkeys(
            item.get("name").strip()
            for item in context_entries
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("name").strip()
            and isinstance(item.get("context"), dict)
            and item["context"].get("cluster") in clusters
        )
    )
    if not contexts:
        raise DomainValidationError("Kubeconfig must define at least one context")
    current_context = payload.get("current-context")
    if not isinstance(current_context, str) or current_context not in contexts:
        current_context = None
    return _KubeconfigMetadata(contexts, current_context)


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin.rstrip("/") != expected:
        raise HTTPException(status_code=403, detail="Kubeconfig import requires a same-origin request")


def _install_problem_handlers(app: FastAPI) -> None:
    """Install local handlers without importing the database-backed API package."""

    @app.exception_handler(DomainError)
    async def domain_problem(_request: Request, error: DomainError) -> JSONResponse:
        return problem(error.status_code, "Domain request failed", str(error), error.code)

    @app.exception_handler(HTTPException)
    async def http_problem(_request: Request, error: HTTPException) -> JSONResponse:
        return problem(error.status_code, "Request failed", error.detail, "request_failed")

    @app.exception_handler(RequestValidationError)
    async def validation_problem(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = []
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            message = str(item.get("msg") or "invalid value")
            details.append(f"{location}: {message}" if location else message)
        return problem(422, "Request validation failed", "; ".join(details), "validation_failed")
