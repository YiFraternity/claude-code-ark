#!/usr/bin/env python3
"""Safety checks for the standalone Claude Ark deployment repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError


BUNDLE_DIR = Path(__file__).parent
LAUNCHER = BUNDLE_DIR / "claude-ark"
INSTALLER = BUNDLE_DIR / "install_claude_ark.sh"
PACKAGER = BUNDLE_DIR / "package_claude_ark.sh"
ARK_PROXY = BUNDLE_DIR / "ark_compat_proxy.py"
MODELHUB_PROXY = BUNDLE_DIR / "modelhub_compat_proxy.py"
REGISTRY = BUNDLE_DIR / "claude_ark_registry.py"
SELECTOR = BUNDLE_DIR / "claude_ark_model_selector.py"
MODEL_TEMPLATE = BUNDLE_DIR / "ak_map_available.example.json"
BOTMUX_TEMPLATE = BUNDLE_DIR / "botmux.example.json"
README = BUNDLE_DIR / "README.md"


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - HTTP handler method name.
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class ClaudeArkPortableBundleTests(unittest.TestCase):
    def test_bundle_contains_all_install_time_assets(self) -> None:
        required = (
            LAUNCHER,
            INSTALLER,
            PACKAGER,
            ARK_PROXY,
            MODELHUB_PROXY,
            REGISTRY,
            SELECTOR,
            MODEL_TEMPLATE,
            BOTMUX_TEMPLATE,
            README,
        )
        self.assertEqual([], [str(path) for path in required if not path.is_file()])
        for shell_script in (LAUNCHER, INSTALLER, PACKAGER):
            subprocess.run(["bash", "-n", str(shell_script)], check=True)

    def test_model_template_has_no_provider_key(self) -> None:
        template = json.loads(MODEL_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(13, len(template))
        self.assertIn("gemini-3.8-flash", template)
        self.assertTrue(all(route["api_keys"] == [""] for route in template.values()))

    def test_modelhub_schema_normalizer_removes_incompatible_patterns(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_tool_schemas

        payload = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Artifact",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "pattern": "^(?!__.*__$)[^\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}\"\\\\./[\\]]{1,200}$",
                                },
                                "metadata": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string", "pattern": "^[a-z]+$"}
                                    },
                                },
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    },
                }
            ]
        }

        normalize_modelhub_tool_schemas(payload)

        parameters = payload["tools"][0]["function"]["parameters"]
        self.assertNotIn("pattern", parameters["properties"]["path"])
        self.assertNotIn("pattern", parameters["properties"]["metadata"]["properties"]["kind"])
        self.assertEqual("string", parameters["properties"]["path"]["type"])
        self.assertEqual(["path"], parameters["required"])
        self.assertFalse(parameters["additionalProperties"])

    def test_botmux_template_routes_claude_code_through_claude_ark_without_credentials(self) -> None:
        template = json.loads(BOTMUX_TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual("claude-code", template["cliId"])
        self.assertEqual("claude-ark", template["wrapperCli"])
        self.assertEqual("claude-ark", template["agentSelectionKey"])
        self.assertTrue(template["disableCliBypass"])
        self.assertEqual("<LARK_APP_ID>", template["larkAppId"])
        self.assertEqual("<LARK_APP_SECRET>", template["larkAppSecret"])

    def test_launcher_is_not_bound_to_a_specific_machine(self) -> None:
        launcher = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("/mnt/bn/", launcher)
        self.assertNotIn("/home/tiger/", launcher)
        self.assertIn("bundle_dir=", launcher)
        self.assertIn("CLAUDE_ARK_MODEL_MAP", launcher)

    def test_installed_symlink_uses_the_real_bundle_directory(self) -> None:
        """The user command may be a symlink, so companion scripts stay beside its target."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle = root / "bundle"
            bin_dir = root / "bin"
            config_dir = root / "config"
            bundle.mkdir()
            bin_dir.mkdir()
            for source in (
                LAUNCHER,
                INSTALLER,
                PACKAGER,
                ARK_PROXY,
                MODELHUB_PROXY,
                REGISTRY,
                SELECTOR,
                MODEL_TEMPLATE,
            ):
                shutil.copy2(source, bundle / source.name)

            installed_launcher = bin_dir / "claude-ark"
            installed_launcher.symlink_to(bundle / "claude-ark")
            model_map = config_dir / "ak_map_available.json"
            config_dir.mkdir()
            model_config = json.loads(MODEL_TEMPLATE.read_text(encoding="utf-8"))
            for route in model_config.values():
                route["api_keys"] = ["test-key"]
            model_map.write_text(json.dumps(model_config), encoding="utf-8")

            servers = [ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler) for _ in range(3)]
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
            for thread in threads:
                thread.start()
            try:
                gateway_port, ark_port, modelhub_port = (server.server_port for server in servers)
                routes_file = config_dir / f"litellm-{gateway_port}.routes"
                with routes_file.open("w", encoding="utf-8") as output:
                    subprocess.run(
                        [sys.executable, str(bundle / "claude_ark_registry.py"), "list", "--map", str(model_map)],
                        check=True,
                        stdout=output,
                    )
                environment = os.environ | {
                    "CLAUDE_ARK_CONFIG_DIR": str(config_dir),
                    "CLAUDE_ARK_MODEL_MAP": str(model_map),
                    "CLAUDE_ARK_PYTHON": sys.executable,
                    "CLAUDE_ARK_LITELLM_BIN": "/bin/true",
                    "CLAUDE_BIN": "/bin/true",
                    "CLAUDE_ARK_PORT": str(gateway_port),
                    "CLAUDE_ARK_COMPAT_PORT": str(ark_port),
                    "CLAUDE_ARK_MODELHUB_PORT": str(modelhub_port),
                }
                result = subprocess.run(
                    [str(installed_launcher), "--model", "gpt-5.5-2026-04-24"],
                    check=False,
                    text=True,
                    capture_output=True,
                    env=environment,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()

    def test_launcher_resolves_bundle_when_invoked_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary = Path(tmpdir)
            launcher_link = temporary / "claude-ark"
            launcher_link.symlink_to(LAUNCHER)
            model_map = temporary / "models.json"
            model_map.write_text(
                json.dumps(
                    {
                        "configured-model": {
                            "provider": "modelhub",
                            "base_url": "https://example.invalid/chat/completions",
                            "api_keys": ["test-secret"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [str(launcher_link), "--model", "missing-model"],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "CLAUDE_ARK_CONFIG_DIR": str(temporary / "config"),
                    "CLAUDE_ARK_LITELLM_BIN": "/bin/true",
                    "CLAUDE_ARK_MODEL_MAP": str(model_map),
                    "CLAUDE_ARK_PYTHON": sys.executable,
                },
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn(
            "selected model is not configured: missing-model",
            completed.stderr,
        )

    def test_plain_launch_uses_a_configured_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary = Path(tmpdir)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_curl.chmod(0o700)
            fake_claude = fake_bin / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$ANTHROPIC_MODEL\"\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o700)
            model_map = temporary / "models.json"
            model_config = json.loads(MODEL_TEMPLATE.read_text(encoding="utf-8"))
            for route in model_config.values():
                route["api_keys"] = ["test-secret"]
            model_map.write_text(json.dumps(model_config), encoding="utf-8")
            with (temporary / "litellm-40127.routes").open("w", encoding="utf-8") as routes:
                subprocess.run(
                    [sys.executable, str(REGISTRY), "list", "--map", str(model_map)],
                    check=True,
                    stdout=routes,
                )
            completed = subprocess.run(
                [str(LAUNCHER)],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "CLAUDE_ARK_CONFIG_DIR": str(temporary),
                    "CLAUDE_ARK_LITELLM_BIN": "/bin/true",
                    "CLAUDE_ARK_MODEL_MAP": str(model_map),
                    "CLAUDE_ARK_PYTHON": sys.executable,
                    "CLAUDE_BIN": str(fake_claude),
                },
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "gemini-3.8-flash\n")

    def test_modelhub_nonstream_tool_calls_receive_ids_and_tool_finish_reason(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_response

        upstream_response = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "",
                                "type": "function",
                                "function": {
                                    "name": "echo_value",
                                    "arguments": '{"value":"ok"}',
                                },
                            }
                        ],
                    },
                }
            ]
        }

        normalized = json.loads(
            normalize_modelhub_response(
                json.dumps(upstream_response).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        )

        choice = normalized["choices"][0]
        self.assertEqual("tool_calls", choice["finish_reason"])
        self.assertRegex(choice["message"]["tool_calls"][0]["id"], r"^call_[0-9a-f]{32}$")

    def test_modelhub_stream_tool_calls_reuse_ids_and_finish_as_tool_calls(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_response, restore_modelhub_tool_signatures

        events = [
            {
                "id": "response-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "echo_value", "arguments": "{\"value\":"},
                                }
                            ],
                        },
                    }
                ],
            },
            {
                "id": "response-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "\"ok\"}"},
                                    "signature": "signed-stream-state",
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "response-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "late-conflicting-id",
                                    "function": {"arguments": ""},
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "response-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        tool_signatures: dict[str, str] = {}
        body = "".join(
            [*(f"data: {json.dumps(event)}\n\n" for event in events), "data: [DONE]\n\n"]
        ).encode("utf-8")

        normalized_body = normalize_modelhub_response(body, "text/event-stream", tool_signatures)
        normalized_events = [
            json.loads(line.removeprefix("data: "))
            for line in normalized_body.decode("utf-8").splitlines()
            if line.startswith("data: {")
        ]

        first_id = normalized_events[0]["choices"][0]["delta"]["tool_calls"][0]["id"]
        late_id = normalized_events[2]["choices"][0]["delta"]["tool_calls"][0]["id"]
        self.assertRegex(first_id, r"^call_[0-9a-f]{32}$")
        self.assertNotIn("id", normalized_events[1]["choices"][0]["delta"]["tool_calls"][0])
        self.assertEqual(first_id, late_id)
        self.assertEqual("tool_calls", normalized_events[3]["choices"][0]["finish_reason"])
        followup_request = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": first_id,
                            "type": "function",
                            "function": {"name": "echo_value", "arguments": '{"value":"ok"}'},
                        }
                    ],
                }
            ]
        }
        restore_modelhub_tool_signatures(followup_request, tool_signatures)
        self.assertEqual(
            "signed-stream-state",
            followup_request["messages"][0]["tool_calls"][0]["signature"],
        )
        self.assertTrue(normalized_body.endswith(b"data: [DONE]\n\n"))

    def test_modelhub_tool_signatures_are_restored_on_the_followup_request(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_response, restore_modelhub_tool_signatures

        tool_signatures: dict[str, str] = {}
        upstream_response = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "",
                                "type": "function",
                                "function": {
                                    "name": "echo_value",
                                    "arguments": '{"value":"ok"}',
                                },
                                "signature": "signed-model-state",
                            }
                        ],
                    },
                }
            ]
        }
        normalized = json.loads(
            normalize_modelhub_response(
                json.dumps(upstream_response).encode("utf-8"),
                "application/json",
                tool_signatures,
            )
        )
        call_id = normalized["choices"][0]["message"]["tool_calls"][0]["id"]
        followup_request = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "echo_value",
                                "arguments": '{"value":"ok"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "ok"},
            ]
        }

        restore_modelhub_tool_signatures(followup_request, tool_signatures)

        restored_call = followup_request["messages"][0]["tool_calls"][0]
        self.assertEqual("signed-model-state", restored_call["signature"])
        self.assertNotIn("signature", followup_request["messages"][0])

    def test_modelhub_text_responses_are_not_rewritten(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_response

        upstream_response = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ]
        }
        body = json.dumps(upstream_response, separators=(",", ":")).encode("utf-8")

        normalized = normalize_modelhub_response(body, "application/json")

        self.assertEqual(body, normalized)

    def test_modelhub_tool_normalization_removes_schema_dialect_markers(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_tool_schemas

        payload = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "parameters": {
                            "$schema": "https://json-schema.org/draft/2020-12/schema",
                            "type": "object",
                            "properties": {
                                "path": {
                                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                                    "type": "string",
                                }
                            },
                        },
                    },
                }
            ]
        }

        normalize_modelhub_tool_schemas(payload)

        parameters = payload["tools"][0]["function"]["parameters"]
        self.assertNotIn("$schema", parameters)
        self.assertNotIn("$schema", parameters["properties"]["path"])
        self.assertEqual(parameters["properties"]["path"]["type"], "string")

    def test_modelhub_tool_normalization_converts_unsupported_constraints(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_tool_schemas

        payload = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Edit",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "mode": {"const": "safe"},
                                "count": {
                                    "type": "integer",
                                    "exclusiveMinimum": 0,
                                },
                                "values": {
                                    "type": "object",
                                    "propertyNames": {"type": "string"},
                                },
                            },
                        },
                    },
                }
            ]
        }

        normalize_modelhub_tool_schemas(payload)

        properties = payload["tools"][0]["function"]["parameters"]["properties"]
        self.assertEqual(properties["mode"], {"enum": ["safe"]})
        self.assertEqual(properties["count"], {"type": "integer"})
        self.assertEqual(properties["values"], {"type": "object"})

    def test_modelhub_tool_normalization_adds_items_to_tuple_arrays(self) -> None:
        from modelhub_compat_proxy import normalize_modelhub_tool_schemas

        prefix_items = [
            {"type": "string"},
            {"type": "string", "enum": ["eq", "ne", "in"]},
            {},
        ]
        payload = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ToolSearch",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "object",
                                    "properties": {
                                        "where": {
                                            "type": "array",
                                            "items": {
                                                "type": "array",
                                                "prefixItems": prefix_items,
                                            },
                                        }
                                    },
                                }
                            },
                        },
                    },
                }
            ]
        }

        normalize_modelhub_tool_schemas(payload)

        inner_array = payload["tools"][0]["function"]["parameters"]["properties"]["query"]["properties"]["where"]["items"]
        self.assertIn("items", inner_array, "ModelHub requires items on every array schema")
        self.assertEqual({}, inner_array["items"])
        self.assertEqual(prefix_items, inner_array["prefixItems"])

    def test_modelhub_http_errors_preserve_the_upstream_response_body(self) -> None:
        import modelhub_compat_proxy

        self.assertTrue(
            hasattr(modelhub_compat_proxy, "modelhub_http_error_response"),
            "ModelHub HTTP error responses must preserve upstream diagnostics",
        )
        modelhub_http_error_response = modelhub_compat_proxy.modelhub_http_error_response

        upstream_body = b'{"error":{"message":"unsupported tool schema keyword"}}'
        error = HTTPError(
            url="https://modelhub.example/chat/completions",
            code=400,
            msg="Bad Request",
            hdrs={"Content-Type": "application/json; charset=utf-8"},
            fp=BytesIO(upstream_body),
        )

        with error:
            body, content_type = modelhub_http_error_response(error)

        self.assertEqual(upstream_body, body)
        self.assertEqual("application/json; charset=utf-8", content_type)

    def test_packager_emits_only_the_safe_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "claude-ark-portable.tar.gz"
            subprocess.run(["bash", str(PACKAGER), str(archive)], check=True)
            with tarfile.open(archive, "r:gz") as bundle:
                names = set(bundle.getnames())
        self.assertEqual(
            {
                "claude-ark/claude-ark",
                "claude-ark/install_claude_ark.sh",
                "claude-ark/package_claude_ark.sh",
                "claude-ark/ark_compat_proxy.py",
                "claude-ark/modelhub_compat_proxy.py",
                "claude-ark/claude_ark_registry.py",
                "claude-ark/claude_ark_model_selector.py",
                "claude-ark/ak_map_available.example.json",
                "claude-ark/botmux.example.json",
                "claude-ark/README.md",
            },
            names,
        )


if __name__ == "__main__":
    unittest.main()
