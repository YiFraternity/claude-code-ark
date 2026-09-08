#!/usr/bin/env python3
"""Loopback OpenAI Chat Completions proxy for JSON-configured ModelHub routes."""

from __future__ import annotations

import argparse
import http.server
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ModelHubRoute:
    base_url: str
    api_key: str


def load_modelhub_routes(map_path: Path) -> dict[str, ModelHubRoute]:
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("model map root must be an object")
    routes: dict[str, ModelHubRoute] = {}
    for model_name, route in mapping.items():
        if not isinstance(route, dict) or route.get("provider") != "modelhub":
            continue
        base_url = route.get("base_url")
        api_keys = route.get("api_keys")
        if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://")):
            raise ValueError(f"invalid ModelHub base_url for model: {model_name}")
        if not isinstance(api_keys, list) or not api_keys or not isinstance(api_keys[0], str) or not api_keys[0]:
            raise ValueError(f"missing ModelHub api_keys for model: {model_name}")
        routes[model_name] = ModelHubRoute(base_url=base_url, api_key=api_keys[0])
    return routes


def normalize_modelhub_tool_schemas(payload: dict[str, object]) -> None:
    """Make Claude Code's unconstrained Workflow.args schema acceptable to ModelHub."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict) or function.get("name") != "Workflow":
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            continue
        arguments = properties.get("args")
        if not isinstance(arguments, dict) or "type" in arguments:
            continue
        # Claude Code intentionally leaves this field unconstrained. ModelHub
        # requires a concrete JSON Schema type for every tool property.
        arguments["type"] = "object"
        arguments["additionalProperties"] = True


class ModelHubCompatibilityHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, ModelHubRoute] = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/health/liveliness":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": {"message": "invalid JSON request"}})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            self._send_json(400, {"error": {"message": "model is required"}})
            return

        selected_model = payload["model"].removeprefix("chat_completions/")
        route = self.routes.get(selected_model)
        if route is None:
            self._send_json(404, {"error": {"message": "configured ModelHub model not found"}})
            return

        payload["model"] = selected_model
        for client_only_field in ("user", "prompt_cache_key", "stream_options"):
            payload.pop(client_only_field, None)
        # ModelHub supports the standard OpenAI tool protocol. Preserve tools,
        # tool_choice, assistant tool_calls, and tool-result messages so Claude
        # Code can execute tasks rather than falling back to text-only replies.
        normalize_modelhub_tool_schemas(payload)
        upstream_request = Request(
            route.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {route.api_key}"},
            method="POST",
        )
        try:
            with urlopen(upstream_request, timeout=90) as upstream:
                body = upstream.read()
                self.send_response(upstream.status)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except HTTPError as error:
            self._send_json(error.code, {"error": {"message": f"ModelHub upstream returned HTTP {error.code}"}})
        except URLError:
            self._send_json(502, {"error": {"message": "ModelHub upstream network error"}})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--map", type=Path, required=True)
    args = parser.parse_args()

    ModelHubCompatibilityHandler.routes = load_modelhub_routes(args.map)
    server = http.server.ThreadingHTTPServer((args.host, args.port), ModelHubCompatibilityHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
