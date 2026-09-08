#!/usr/bin/env python3
"""Safe model registry and LiteLLM runtime-config generator for Claude Ark."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml


ARK_CHAT_COMPLETIONS = "ark_chat_completions"
ARK_RESPONSES = "ark_responses"
MODELHUB = "modelhub"
SUPPORTED_PROVIDERS = {ARK_CHAT_COMPLETIONS, ARK_RESPONSES, MODELHUB}


@dataclass(frozen=True)
class ModelRoute:
    """Non-secret model metadata used to create one LiteLLM route."""

    name: str
    provider: str
    base_url: str
    endpoint_id: str
    api_key_env: str


def key_env_name(model_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", model_name).upper().strip("_")
    return f"CLAUDE_ARK_KEY_{normalized}"


def _validate_route(model_name: str, route: object) -> ModelRoute:
    if not isinstance(route, dict):
        raise ValueError(f"model route must be an object: {model_name}")
    provider = route.get("provider")
    base_url = route.get("base_url")
    api_keys = route.get("api_keys")
    endpoint_id = route.get("endpoint_id", "")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider for model: {model_name}")
    if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://")):
        raise ValueError(f"invalid base_url for model: {model_name}")
    if not isinstance(api_keys, list) or not api_keys or not isinstance(api_keys[0], str) or not api_keys[0]:
        raise ValueError(f"missing api_keys for model: {model_name}")
    if provider in {ARK_CHAT_COMPLETIONS, ARK_RESPONSES}:
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError(f"missing endpoint_id for Ark model: {model_name}")
    elif endpoint_id not in ("", None):
        raise ValueError(f"unexpected endpoint_id for ModelHub model: {model_name}")
    return ModelRoute(
        name=model_name,
        provider=provider,
        base_url=base_url.rstrip("/"),
        endpoint_id=endpoint_id or "",
        api_key_env=key_env_name(model_name),
    )


def load_routes(map_path: Path) -> list[ModelRoute]:
    """Load every configured route while deliberately leaving key values behind."""
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("model map root must be an object")
    routes = [_validate_route(model_name, route) for model_name, route in mapping.items()]
    env_names = [route.api_key_env for route in routes]
    if len(env_names) != len(set(env_names)):
        raise ValueError("two model names normalize to the same key environment variable")
    return routes


def export_route_keys(map_path: Path, routes: Sequence[ModelRoute]) -> None:
    """Load only each configured primary key into its named process environment variable."""
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    for route in routes:
        raw_route = mapping.get(route.name)
        if not isinstance(raw_route, dict):
            raise ValueError(f"model disappeared from the map: {route.name}")
        api_keys = raw_route.get("api_keys")
        if not isinstance(api_keys, list) or not api_keys or not isinstance(api_keys[0], str) or not api_keys[0]:
            raise ValueError(f"missing api_keys for model: {route.name}")
        os.environ[route.api_key_env] = api_keys[0]


def _litellm_params(route: ModelRoute, ark_compat_base_url: str, modelhub_compat_base_url: str) -> dict[str, object]:
    params: dict[str, object] = {"api_key": f"os.environ/{route.api_key_env}", "drop_params": True}
    if route.provider == ARK_CHAT_COMPLETIONS:
        params.update({"model": f"openai/chat_completions/{route.endpoint_id}", "api_base": ark_compat_base_url})
    elif route.provider == ARK_RESPONSES:
        params.update({"model": f"openai/{route.endpoint_id}", "api_base": ark_compat_base_url})
    elif route.provider == MODELHUB:
        params.update({"model": f"openai/chat_completions/{route.name}", "api_base": modelhub_compat_base_url})
    else:  # pragma: no cover - load_routes validates providers first.
        raise ValueError(f"unsupported provider: {route.provider}")
    return params


def write_litellm_config(
    routes: Sequence[ModelRoute],
    output_path: Path,
    *,
    ark_compat_base_url: str,
    modelhub_compat_base_url: str,
) -> None:
    """Write an owner-only config that references environment names, never key values."""
    document = {
        "model_list": [
            {"model_name": route.name, "litellm_params": _litellm_params(route, ark_compat_base_url, modelhub_compat_base_url)}
            for route in routes
        ],
        "litellm_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY", "drop_params": True},
        "router_settings": {"num_retries": 0},
    }
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    output_path.chmod(0o600)


def _route_by_name(routes: Sequence[ModelRoute], model_name: str) -> ModelRoute | None:
    return next((route for route in routes if route.name == model_name), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    list_parser = subcommands.add_parser("list", help="print model name and provider only")
    list_parser.add_argument("--map", type=Path, required=True)

    contains_parser = subcommands.add_parser("contains", help="check a configured model name")
    contains_parser.add_argument("--map", type=Path, required=True)
    contains_parser.add_argument("--model", required=True)

    run_parser = subcommands.add_parser("run-litellm", help="generate config, load keys, then exec LiteLLM")
    run_parser.add_argument("--map", type=Path, required=True)
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--ark-compat-base-url", required=True)
    run_parser.add_argument("--modelhub-compat-base-url", required=True)
    run_parser.add_argument("--litellm-binary", required=True)
    run_parser.add_argument("--host", required=True)
    run_parser.add_argument("--port", required=True)
    args = parser.parse_args()

    routes = load_routes(args.map)
    if args.command == "list":
        for route in routes:
            print(f"{route.name}\t{route.provider}")
        return 0
    if args.command == "contains":
        return 0 if _route_by_name(routes, args.model) is not None else 1
    if args.command == "run-litellm":
        export_route_keys(args.map, routes)
        write_litellm_config(
            routes,
            args.config,
            ark_compat_base_url=args.ark_compat_base_url,
            modelhub_compat_base_url=args.modelhub_compat_base_url,
        )
        command = [
            args.litellm_binary,
            "--config",
            str(args.config),
            "--host",
            args.host,
            "--port",
            args.port,
            "--telemetry",
            "False",
        ]
        os.execvpe(args.litellm_binary, command, os.environ)
    raise AssertionError(f"unexpected command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
