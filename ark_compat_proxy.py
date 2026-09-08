#!/usr/bin/env python3
"""Loopback-only compatibility proxy for strict Ark OpenAI-compatible APIs."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


FORWARDED_PATHS = {"/responses", "/chat/completions"}
HOP_BY_HOP_HEADERS = {"connection", "content-length", "host", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


class ArkCompatibilityHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_base_url: ClassVar[str]

    def log_message(self, format: str, *args: object) -> None:
        """Avoid logging prompts or headers to disk."""

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/health/liveliness":
            self.send_error(404)
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path not in FORWARDED_PATHS:
            self.send_error(404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "request body must be JSON")
            return
        if not isinstance(payload, dict):
            self.send_error(400, "request body must be a JSON object")
            return

        # Ark's Responses implementation rejects the OpenAI `user` metadata field.
        payload.pop("user", None)
        if isinstance(payload.get("model"), str):
            payload["model"] = payload["model"].removeprefix("chat_completions/")
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        }
        request_headers["Content-Length"] = str(len(request_body))
        upstream_request = Request(
            f"{self.upstream_base_url}{self.path}",
            data=request_body,
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(upstream_request, timeout=600) as upstream_response:
                status = upstream_response.status
                response_headers = upstream_response.headers.items()
                response_body = upstream_response.read()
        except HTTPError as error:
            status = error.code
            response_headers = error.headers.items()
            response_body = error.read()
        except OSError:
            self.send_error(502, "Ark upstream is unavailable")
            return

        self.send_response(status)
        for name, value in response_headers:
            if name.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response_body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream-base-url", required=True)
    args = parser.parse_args()

    ArkCompatibilityHandler.upstream_base_url = args.upstream_base_url.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), ArkCompatibilityHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
