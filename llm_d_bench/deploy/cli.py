"""Command-line entrance for the namespace-scoped llm-d deployer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm_d_bench.core.exceptions import DomainError
from llm_d_bench.deploy.namespace_deployer import (
    DeploymentCommandError,
    NamespaceDeployer,
    RenderedPlan,
)
from llm_d_bench.utils.shell import CommandNotFoundError, CommandTimeoutError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmd", description="Deploy llm-d stacks in an existing namespace")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="List Guide Kustomize overlays")
    _add_source(catalog)
    catalog.add_argument(
        "--all",
        dest="include_unsupported",
        action="store_true",
        help="Include internal or unsupported overlays and their rejection reasons",
    )
    _add_output(catalog)

    render = subparsers.add_parser("render", help="Render and preflight a stack without changing it")
    _add_source(render)
    _add_connection(render)
    _add_deployment(render)
    _add_output(render, yaml_output=True)

    deploy = subparsers.add_parser("deploy", help="Deploy a stack into an existing namespace")
    _add_source(deploy)
    _add_connection(deploy)
    _add_deployment(deploy)
    _add_output(deploy)

    list_command = subparsers.add_parser("list", help="List llmd inventories in a namespace")
    _add_connection(list_command)
    list_command.add_argument("--namespace", required=True)
    _add_output(list_command)

    status = subparsers.add_parser("status", help="Inspect one inventoried stack")
    _add_connection(status)
    _add_identity(status)
    _add_output(status)

    teardown = subparsers.add_parser("teardown", help="Remove one inventoried stack")
    _add_connection(teardown)
    _add_identity(teardown)
    _add_output(teardown)

    ui = subparsers.add_parser("ui", help="Run the database-free local Lens entrance")
    _add_source(ui)
    _add_connection(ui)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8000)
    ui.add_argument("--web-root", type=Path, help="Built Lens frontend directory (defaults to dist/)")
    return parser


def _add_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", type=Path, help="Local llm-d checkout (or set LLM_D_ROOT)")


def _add_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kubeconfig", type=Path, help="Kubeconfig used only by this process")
    parser.add_argument("--context", help="Kubeconfig context")


def _add_deployment(parser: argparse.ArgumentParser) -> None:
    _add_identity(parser)
    parser.add_argument("--guide", required=True)
    parser.add_argument("--overlay", required=True, help="Catalog overlay path relative to the source checkout")
    parser.add_argument(
        "--model",
        help="Hugging Face model ID (defaults to the model declared by the selected Guide overlay)",
    )


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)


def _add_output(parser: argparse.ArgumentParser, *, yaml_output: bool = False) -> None:
    choices = ["text", "json", *(["yaml"] if yaml_output else [])]
    parser.add_argument("--output", choices=choices, default="text")


def _deployer(args: argparse.Namespace) -> NamespaceDeployer:
    return NamespaceDeployer(
        getattr(args, "source", None),
        kubeconfig=getattr(args, "kubeconfig", None),
        context=getattr(args, "context", None),
    )


async def _execute(args: argparse.Namespace) -> tuple[Any, str | None]:
    deployer = _deployer(args)
    if args.command == "catalog":
        items = [asdict(item) for item in deployer.catalog()]
        if not args.include_unsupported:
            items = [item for item in items if item["supported"]]
        return items, None
    if args.command == "render":
        plan = await deployer.render(
            namespace=args.namespace,
            name=args.name,
            guide=args.guide,
            overlay=args.overlay,
            model=args.model,
            preflight=True,
        )
        return plan.summary(), _rendered_yaml(plan)
    if args.command == "deploy":
        inventory = await deployer.deploy(
            namespace=args.namespace,
            name=args.name,
            guide=args.guide,
            overlay=args.overlay,
            model=args.model,
        )
        return {**inventory.payload(), "status": "installed"}, None
    if args.command == "list":
        return await deployer.list(args.namespace), None
    if args.command == "status":
        return await deployer.status(args.namespace, args.name), None
    if args.command == "teardown":
        return await deployer.teardown(args.namespace, args.name), None
    raise ValueError(f"unsupported command: {args.command}")


def _rendered_yaml(plan: RenderedPlan) -> str:
    return (
        "# Router resources (preflight only; Helm owns installation)\n"
        f"{plan.router_manifest}\n---\n{plan.model_manifest}"
    )


def _print_result(payload: Any, output: str, yaml_payload: str | None = None) -> None:
    if output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if output == "yaml":
        print(yaml_payload or "")
        return
    if isinstance(payload, list):
        if not payload:
            print("No entries found.")
            return
        if all(isinstance(item, dict) and "supported" in item for item in payload):
            print("GUIDE\tOVERLAY\tDEPLOYER SUPPORT\tUPSTREAM MATURITY\tREASON")
            for item in payload:
                support = "supported" if item.get("supported") else "unsupported"
                maturity = item.get("maturity") or "-"
                reason = item.get("reason") or "-"
                print(f"{item['guide']}\t{item['overlay']}\t{support}\t{maturity}\t{reason}")
            return
        for item in payload:
            print(f"{item.get('namespace', '')}/{item.get('name', '')}\t{item.get('status', '')}")
        return
    if isinstance(payload, dict):
        identity = "/".join(part for part in (payload.get("namespace"), payload.get("name")) if part)
        status = payload.get("status")
        if identity and status:
            print(f"{identity}: {status}")
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(payload)


def _run_ui(args: argparse.Namespace) -> int:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.host not in loopback:
        print("llmd ui is local-only and must bind to a loopback address.", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("port must be between 1 and 65535", file=sys.stderr)
        return 2
    from llm_d_bench.deploy.local_api import create_local_app

    deployer = _deployer(args)
    deployer.catalog()  # Validate the source before starting a long-running server.
    app = create_local_app(deployer, web_root=args.web_root)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ui":
            return _run_ui(args)
        payload, yaml_payload = asyncio.run(_execute(args))
        _print_result(payload, args.output, yaml_payload)
        return 0
    except (DomainError, DeploymentCommandError, CommandNotFoundError, CommandTimeoutError, ValueError) as error:
        print(f"llmd: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
