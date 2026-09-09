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
from pathlib import Path


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
        self.assertEqual(12, len(template))
        self.assertTrue(all(route["api_keys"] == [""] for route in template.values()))

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
