from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEVELOPER = ROOT / "swarm_router" / "developer.py"


class DeveloperGrepRetryGuidanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = DEVELOPER.read_text(
            encoding="utf-8",
        )

    def test_retry_quotes_rejected_command(self) -> None:
        self.assertIn(
            'f"Rejected command: `{rejected_command}`. "',
            self.source,
        )

    def test_retry_retains_pipeline_warning(self) -> None:
        self.assertIn(
            '"commands. Do not use a shell pipeline. "',
            self.source,
        )

    def test_retry_explicitly_forbids_plain_grep(self) -> None:
        self.assertIn(
            '"The `grep` executable is never approved for this role, including "',
            self.source,
        )
        self.assertIn(
            '"plain `grep -n PATTERN PATH`. For a bounded source search, use "',
            self.source,
        )

    def test_retry_supplies_plain_grep_equivalent(self) -> None:
        self.assertIn(
            '"`rg -n -m N \'PATTERN\' PATH`. Therefore replace plain "',
            self.source,
        )
        self.assertIn(
            '"`grep -n PATTERN PATH` with "',
            self.source,
        )
        self.assertIn(
            '"`rg -n -m 200 \'PATTERN\' PATH`, and replace "',
            self.source,
        )

    def test_retry_retains_piped_grep_equivalent(self) -> None:
        self.assertIn(
            '"`grep PATTERN PATH | head -N` with "',
            self.source,
        )
        self.assertIn(
            '"`rg -n -m N \'PATTERN\' PATH`. "',
            self.source,
        )

    def test_retry_warns_not_to_repeat_executable(self) -> None:
        self.assertIn(
            '"Do not repeat the rejected executable. For repository status, "',
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
