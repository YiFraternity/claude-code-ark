#!/usr/bin/env python3
"""Loopback OpenAI Chat Completions proxy for JSON-configured ModelHub routes."""

from __future__ import annotations

import argparse
import http.server
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import ClassVar
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_CACHED_TOOL_SIGNATURES = 4096


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


def modelhub_http_error_response(error: HTTPError) -> tuple[bytes, str]:
    """Preserve ModelHub's diagnostic body while retaining a safe fallback."""
    body = error.read()
    if not body:
        body = json.dumps(
            {"error": {"message": f"ModelHub upstream returned HTTP {error.code}"}},
            ensure_ascii=False,
        ).encode("utf-8")
    content_type = error.headers.get("Content-Type", "application/json") if error.headers else "application/json"
    return body, content_type


def strip_modelhub_unsupported_schema_patterns(schema: object) -> None:
    """Remove regex constraints ModelHub cannot parse from one tool JSON Schema."""
    if isinstance(schema, dict):
        # Claude Code's Artifact tool uses ECMAScript Unicode-property syntax
        # (for example ``\\p{Cc}``). ModelHub validates it as a different regex
        # dialect and rejects the complete request before model execution.
        schema.pop("pattern", None)
        for value in schema.values():
            strip_modelhub_unsupported_schema_patterns(value)
    elif isinstance(schema, list):
        for value in schema:
            strip_modelhub_unsupported_schema_patterns(value)


def normalize_modelhub_tool_schemas(payload: dict[str, object]) -> None:
    """Normalize Claude Code tool schemas for ModelHub without changing the tool protocol."""

    def remove_schema_markers(value: object) -> None:
        if isinstance(value, dict):
            value.pop("$schema", None)
            if "const" in value:
                value["enum"] = [value.pop("const")]
            value.pop("exclusiveMinimum", None)
            value.pop("propertyNames", None)
            if value.get("type") == "array" and "items" not in value:
                value["items"] = {}
            for nested in value.values():
                remove_schema_markers(nested)
        elif isinstance(value, list):
            for nested in value:
                remove_schema_markers(nested)

    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue
        strip_modelhub_unsupported_schema_patterns(parameters)
        remove_schema_markers(parameters)
        if function.get("name") != "Workflow":
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


def _normalize_tool_call_choices(
    payload: dict[str, object],
    call_ids: dict[tuple[object, object], str],
    choices_with_tool_calls: set[object],
    tool_signatures: dict[str, str] | None,
) -> bool:
    """Repair ModelHub's incomplete OpenAI tool-call metadata in one payload."""
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False

    changed = False
    for choice_position, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        choice_index = choice.get("index", choice_position)
        if not isinstance(choice_index, (int, str)):
            choice_index = choice_position

        for container_name in ("message", "delta"):
            container = choice.get(container_name)
            if not isinstance(container, dict):
                continue
            tool_calls = container.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                continue
            choices_with_tool_calls.add(choice_index)
            for call_position, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                call_index = tool_call.get("index", call_position)
                if not isinstance(call_index, (int, str)):
                    call_index = call_position
                call_key = (choice_index, call_index)
                call_id = tool_call.get("id")
                normalized_call_id = call_ids.get(call_key)
                if normalized_call_id is not None:
                    if isinstance(call_id, str) and call_id and call_id != normalized_call_id:
                        tool_call["id"] = normalized_call_id
                        changed = True
                    elif call_id == "":
                        tool_call["id"] = normalized_call_id
                        changed = True
                elif isinstance(call_id, str) and call_id:
                    normalized_call_id = call_id
                    call_ids[call_key] = normalized_call_id
                elif call_id == "":
                    normalized_call_id = f"call_{uuid4().hex}"
                    call_ids[call_key] = normalized_call_id
                    tool_call["id"] = normalized_call_id
                    changed = True
                signature = tool_call.get("signature")
                if (
                    tool_signatures is not None
                    and isinstance(normalized_call_id, str)
                    and normalized_call_id
                    and isinstance(signature, str)
                    and signature
                ):
                    tool_signatures[normalized_call_id] = signature
                    while len(tool_signatures) > MAX_CACHED_TOOL_SIGNATURES:
                        del tool_signatures[next(iter(tool_signatures))]

        if choice.get("finish_reason") == "stop" and choice_index in choices_with_tool_calls:
            choice["finish_reason"] = "tool_calls"
            changed = True
    return changed


def restore_modelhub_tool_signatures(
    payload: dict[str, object],
    tool_signatures: dict[str, str],
) -> None:
    """Restore Gemini signatures that generic Anthropic adapters cannot retain."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = tool_call.get("id")
            if not isinstance(call_id, str):
                continue
            signature = tool_signatures.get(call_id)
            if signature and not tool_call.get("signature"):
                tool_call["signature"] = signature


def normalize_modelhub_response(
    body: bytes,
    content_type: str,
    tool_signatures: dict[str, str] | None = None,
) -> bytes:
    """Make ModelHub tool-call responses valid for OpenAI-compatible clients."""
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type == "application/json":
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if not isinstance(payload, dict):
            return body
        if not _normalize_tool_call_choices(payload, {}, set(), tool_signatures):
            return body
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if media_type != "text/event-stream":
        return body
    try:
        lines = body.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return body

    call_ids: dict[tuple[object, object], str] = {}
    choices_with_tool_calls: set[object] = set()
    normalized_lines: list[str] = []
    changed = False
    for line in lines:
        if line.endswith("\r\n"):
            content, line_ending = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            content, line_ending = line[:-1], line[-1]
        else:
            content, line_ending = line, ""

        if not content.startswith("data:"):
            normalized_lines.append(line)
            continue
        event_text = content.removeprefix("data:").lstrip()
        if event_text == "[DONE]":
            normalized_lines.append(line)
            continue
        try:
            event = json.loads(event_text)
        except json.JSONDecodeError:
            normalized_lines.append(line)
            continue
        if not isinstance(event, dict) or not _normalize_tool_call_choices(
            event,
            call_ids,
            choices_with_tool_calls,
            tool_signatures,
        ):
            normalized_lines.append(line)
            continue
        normalized_event = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        normalized_lines.append(f"data: {normalized_event}{line_ending}")
        changed = True

    return "".join(normalized_lines).encode("utf-8") if changed else body


class ModelHubCompatibilityHandler(http.server.BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, ModelHubRoute]] = {}
    tool_signatures: ClassVar[dict[str, str]] = {}
    tool_signatures_lock: ClassVar[Lock] = Lock()

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
        with self.tool_signatures_lock:
            restore_modelhub_tool_signatures(payload, self.tool_signatures)
        upstream_request = Request(
            route.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {route.api_key}"},
            method="POST",
        )
        try:
            with urlopen(upstream_request, timeout=90) as upstream:
                body = upstream.read()
                content_type = upstream.headers.get("Content-Type", "application/json")
                with self.tool_signatures_lock:
                    body = normalize_modelhub_response(body, content_type, self.tool_signatures)
                self.send_response(upstream.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except HTTPError as error:
            body, content_type = modelhub_http_error_response(error)
            self.send_response(error.code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
