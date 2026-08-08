from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from swarm_router.btl_tools import BTLTools, READ_TOOLS, WRITE_TOOLS


class TestBTLTools(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init"], cwd=self.root, capture_output=True, check=True)
        (self.root / "code.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "code.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base"],
            cwd=self.root, capture_output=True, check=True,
        )
        self.tools = BTLTools(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_profiles_have_no_shell_or_git_mutation(self) -> None:
        names = READ_TOOLS | WRITE_TOOLS
        self.assertEqual(WRITE_TOOLS, {"write_file", "replace_text"})
        for forbidden in ("shell", "commit", "push", "checkout", "merge", "reset", "deploy"):
            self.assertFalse(any(forbidden in name for name in names))
        self.assertNotIn("write_file", {item["function"]["name"] for item in self.tools.schemas(False)})

    def test_paths_reject_absolute_traversal_backslash_symlink_and_secrets(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.root / "escape").symlink_to(outside)
        (self.root / ".env").write_text("TOKEN=x", encoding="utf-8")
        for value in (
            str(outside), "../outside.txt", "dir\\file", "escape", ".env",
            ".git/config", "secrets.json", "certificate.pem",
        ):
            with self.assertRaises(ValueError, msg=value):
                self.tools.read_file(value)
            with self.assertRaises(ValueError, msg=value):
                self.tools.write_file(value, "x")

    def test_likely_secret_content_is_not_returned_or_searched(self) -> None:
        (self.root / "ordinary.txt").write_text("token=sk-abcdefghijklmnopqrstuvwxyz\n")
        with self.assertRaises(ValueError):
            self.tools.read_file("ordinary.txt")
        self.assertEqual(self.tools.search_text("token")["matches"], [])

    def test_read_search_write_and_replace(self) -> None:
        self.assertIn("value = 1", self.tools.read_file("code.py")["content"])
        self.assertEqual(self.tools.search_text("value", include="*.py")["matches"][0]["line"], 1)
        self.tools.write_file("pkg/new.py", "answer = 1\n")
        result = self.tools.replace_text("pkg/new.py", "1", "42")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual((self.root / "pkg/new.py").read_text(), "answer = 42\n")

    def test_search_hides_secret_files_and_write_preserves_mode(self) -> None:
        (self.root / ".env.local").write_text("needle=secret\n")
        script = self.root / "script.sh"
        script.write_text("needle=old\n")
        script.chmod(0o755)
        matches = self.tools.search_text("needle")["matches"]
        self.assertEqual([item["path"] for item in matches], ["script.sh"])
        self.tools.write_file("script.sh", "needle=new\n")
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)

    def test_read_profile_rejects_write_dispatch(self) -> None:
        with self.assertRaises(ValueError):
            self.tools.dispatch("write_file", {"path": "x", "content": "x"}, writable=False)

    def test_git_diff_has_no_model_controlled_target(self) -> None:
        self.tools.write_file("code.py", "value = 2\n")
        output = self.tools.git_diff()["output"]
        self.assertIn("value = 2", output)
        with self.assertRaises(TypeError):
            self.tools.git_diff(target="--output=/tmp/escape")

    def test_dispatch_returns_json_and_rejects_extra_args(self) -> None:
        self.assertIn("entries", json.loads(self.tools.dispatch("list_files", {}, writable=False)))
        with self.assertRaises(ValueError):
            self.tools.dispatch("read_file", {"path": "code.py", "extra": True}, writable=False)


if __name__ == "__main__":
    unittest.main()
