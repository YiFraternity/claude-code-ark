#!/usr/bin/env python3
"""Safety checks for the standalone Claude Ark deployment repository."""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
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
