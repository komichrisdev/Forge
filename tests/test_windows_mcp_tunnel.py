from __future__ import annotations

from pathlib import Path
import re
import unittest


class WindowsMcpTunnelDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.start = (cls.root / "docs/windows/start-swarm-mcp-tunnel.ps1").read_text(encoding="utf-8")
        cls.stop = (cls.root / "docs/windows/stop-swarm-mcp-tunnel.ps1").read_text(encoding="utf-8")
        cls.doc = (cls.root / "docs/phase-2.1-windows-mcp-tunnel.md").read_text(encoding="utf-8")
        cls.root_readme = (cls.root / "README.md").read_text(encoding="utf-8")
        cls.app_readme = (cls.root / "chatgpt_app/README.md").read_text(encoding="utf-8")

    def test_start_script_defaults_to_loopback_port_8790(self) -> None:
        self.assertIn("[int]$LocalPort = 8790", self.start)
        self.assertIn("[int]$RemotePort = 8790", self.start)
        self.assertIn("127.0.0.1:$LocalPort:127.0.0.1:$RemotePort", self.start)
        self.assertIn("ExitOnForwardFailure=yes", self.start)
        self.assertIn("ServerAliveInterval=30", self.start)
        self.assertIn("ServerAliveCountMax=3", self.start)

    def test_start_script_validates_the_required_inputs(self) -> None:
        self.assertIn("Get-Command ssh.exe", self.start)
        self.assertIn("Test-Path -LiteralPath $_ -PathType Leaf", self.start)
        self.assertIn("BatchMode", self.start)
        self.assertIn("Get-NetTCPConnection", self.start)
        self.assertIn("Get-CimInstance Win32_Process", self.start)
        self.assertIn("Invoke-RestMethod", self.start)
        self.assertIn("tools/list", self.start)
        self.assertIn("wiki.search", self.start)
        self.assertIn("wiki.page", self.start)
        self.assertIn("wiki.related", self.start)
        self.assertIn("wiki.status", self.start)

    def test_stop_script_only_targets_helper_state(self) -> None:
        self.assertIn("Read-State", self.stop)
        self.assertIn("Stop-Process -Id $pid -Force", self.stop)
        self.assertIn("Get-CimInstance Win32_Process", self.stop)
        self.assertIn("Remove-State", self.stop)
        self.assertNotIn("Get-Credential", self.stop)

    def test_scripts_keep_secrets_out(self) -> None:
        combined = "\n".join([self.start, self.stop, self.doc, self.root_readme, self.app_readme])
        for forbidden in [
            "Get-Credential",
            "Read-Host",
            "password=",
            "token=",
            "BEGIN OPENSSH PRIVATE KEY",
            "BEGIN PRIVATE KEY",
            "0.0.0.0:8790",
        ]:
            self.assertNotIn(forbidden.lower(), combined.lower())

    def test_documentation_states_the_security_boundary(self) -> None:
        for needle in [
            "http://127.0.0.1:8790/mcp",
            "Direct LAN exposure is not enabled",
            "Port `8790` remains loopback-only on Debian",
            "The helper contains no credentials",
            "Users must not commit private keys or tokens",
            "Cursor and Codex client configuration has not begun",
        ]:
            self.assertIn(needle, self.doc)

    def test_readme_mentions_the_supported_tunnel_workflow(self) -> None:
        self.assertIn("docs/windows/start-swarm-mcp-tunnel.ps1", self.root_readme)
        self.assertIn("127.0.0.1:8790/mcp", self.app_readme)

    def test_helper_uses_a_local_state_file_outside_the_repository(self) -> None:
        self.assertTrue(
            any(marker in self.start for marker in ["LOCALAPPDATA", "TEMP"])
        )
        self.assertTrue(
            any(marker in self.stop for marker in ["LOCALAPPDATA", "TEMP"])
        )
