from __future__ import annotations

from dataclasses import replace
from importlib import resources
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from shutil import copyfile
from stat import S_IMODE
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import io
import json
import os
import subprocess
import unittest

from swarm_router.cli import main
from swarm_router.wiki import (
    SCHEMA_VERSION,
    SourceManifest,
    WikiLockError,
    WikiRepository,
    WikiValidationError,
    normalize_jira_key,
    parse_page,
    parse_source,
    serialize_page,
    serialize_source,
)


def _try_lock(root: str, queue: object) -> None:
    repository = WikiRepository(root)
    try:
        with repository.locked(timeout_seconds=0.15):
            queue.put("acquired")  # type: ignore[attr-defined]
    except WikiLockError:
        queue.put("timeout")  # type: ignore[attr-defined]


def _write_title(root: str, title: str, start: object, queue: object) -> None:
    repository = WikiRepository(root)
    page = repository.get_page("acme-orbit-overview")
    start.wait()  # type: ignore[attr-defined]
    try:
        repository.write_page(replace(page, title=title), section="projects")
        queue.put("ok")  # type: ignore[attr-defined]
    except Exception as exc:
        queue.put(type(exc).__name__)  # type: ignore[attr-defined]


class WikiStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "wiki"
        self.repository = WikiRepository(self.root)
        self.repository.initialize(with_samples=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def overview(self) -> Path:
        return self.root / "wiki/projects/acme-orbit-overview.md"

    @property
    def source(self) -> Path:
        return self.root / "sources/manifests/src-orbit-charter-v1.yaml"

    def codes(self) -> list[str]:
        return [issue.code for issue in self.repository.validate()]

    def git_init(self) -> str:
        for args in (
            ("init", "--template=", "-b", "main"),
            ("config", "user.name", "Codex"),
            ("config", "user.email", "codex@local"),
            ("add", "--all"),
            ("commit", "-m", "test wiki"),
        ):
            subprocess.run(
                ["git", "-C", str(self.root), *args],
                check=True, text=True, capture_output=True,
            )
        return subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()

    def test_valid_page(self) -> None:
        page = parse_page(self.overview.read_text(encoding="utf-8"))
        self.assertEqual(page.id, "acme-orbit-overview")
        self.assertEqual(page.confidence, 95)

    def test_valid_source_manifest(self) -> None:
        source = parse_source(self.source.read_text(encoding="utf-8"))
        self.assertEqual(source.source_id, "src-orbit-charter-v1")
        self.assertEqual(source.checksum_algorithm, "sha256")

    def test_missing_page_id(self) -> None:
        self.overview.write_text(
            self.overview.read_text(encoding="utf-8").replace(
                "id: acme-orbit-overview\n", ""
            ),
            encoding="utf-8",
        )
        self.assertIn("missing_field", self.codes())

    def test_duplicate_page_id(self) -> None:
        copyfile(self.overview, self.root / "wiki/research/copied.md")
        self.assertIn("duplicate_page_id", self.codes())

    def test_duplicate_source_id(self) -> None:
        copyfile(self.source, self.root / "sources/manifests/copied.yaml")
        self.assertIn("duplicate_source_id", self.codes())

    def test_malformed_front_matter(self) -> None:
        self.overview.write_text("not-front-matter\n", encoding="utf-8")
        self.assertIn("malformed_front_matter", self.codes())

    def test_invalid_field_type(self) -> None:
        text = self.overview.read_text(encoding="utf-8").replace(
            "aliases:\n  - Orbit training project\n  - Órbita de ejemplo\n",
            "aliases: wrong\n",
        )
        self.overview.write_text(text, encoding="utf-8")
        self.assertIn("invalid_type", self.codes())

    def test_invalid_timestamp(self) -> None:
        self.overview.write_text(
            self.overview.read_text(encoding="utf-8").replace(
                "2026-01-02T12:00:00Z", "not-a-date"
            ),
            encoding="utf-8",
        )
        self.assertIn("invalid_timestamp", self.codes())

    def test_invalid_verification_status(self) -> None:
        self.overview.write_text(
            self.overview.read_text(encoding="utf-8").replace(
                "verification_status: verified", "verification_status: guessed"
            ),
            encoding="utf-8",
        )
        self.assertIn("invalid_verification_status", self.codes())

    def test_invalid_confidence(self) -> None:
        self.overview.write_text(
            self.overview.read_text(encoding="utf-8").replace(
                "confidence: 95", "confidence: 101"
            ),
            encoding="utf-8",
        )
        self.assertIn("invalid_confidence", self.codes())

    def test_broken_internal_page_link(self) -> None:
        self.overview.write_text(
            self.overview.read_text(encoding="utf-8").replace(
                "[[page:acme-orbit-cache-decision]]", "[[page:missing-page]]"
            ),
            encoding="utf-8",
        )
        self.assertIn("broken_page_link", self.codes())

    def test_broken_source_reference(self) -> None:
        text = self.overview.read_text(encoding="utf-8")
        text = text.replace("src-orbit-charter-v1", "src-missing-v1")
        self.overview.write_text(text, encoding="utf-8")
        self.assertIn("broken_source_ref", self.codes())

    def test_missing_superseded_page(self) -> None:
        page = parse_page(self.overview.read_text(encoding="utf-8"))
        self.overview.write_text(
            serialize_page(replace(page, supersedes=("missing-page",))),
            encoding="utf-8",
        )
        self.assertIn("missing_superseded_page", self.codes())

    def test_supersession_cycle(self) -> None:
        decision_path = self.root / "wiki/features/acme-orbit-cache-decision.md"
        overview = parse_page(self.overview.read_text(encoding="utf-8"))
        decision = parse_page(decision_path.read_text(encoding="utf-8"))
        self.overview.write_text(serialize_page(replace(
            overview, verification_status="superseded",
            supersedes=(decision.id,),
        )), encoding="utf-8")
        decision_path.write_text(serialize_page(replace(
            decision, verification_status="superseded",
            supersedes=(overview.id,),
        )), encoding="utf-8")
        self.assertIn("supersession_cycle", self.codes())

    def test_traversal_style_page_id(self) -> None:
        page = parse_page(self.overview.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(WikiValidationError, "invalid_page_id"):
            parse_page(serialize_page(replace(page, id="../escape")))

    def test_traversal_style_source_locator(self) -> None:
        source = parse_source(self.source.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(WikiValidationError, "unsafe_locator"):
            parse_source(serialize_source(replace(source, locator="../private.txt")))

    def test_symlink_escape(self) -> None:
        (self.root / "wiki/projects/escape.md").symlink_to("/etc/passwd")
        self.assertIn("symlink_forbidden", self.codes())

    def test_immutable_source_overwrite_attempt(self) -> None:
        source = parse_source(self.source.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            self.repository.write_source(source)

    def test_atomic_page_replacement(self) -> None:
        page = self.repository.get_page("acme-orbit-overview")
        target = self.repository.write_page(
            replace(page, title="Acme Orbit revised"), section="projects"
        )
        self.assertEqual(self.repository.get_page(page.id).title, "Acme Orbit revised")
        self.assertEqual(S_IMODE(target.stat().st_mode), 0o640)
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_failed_validation_leaves_original_unchanged(self) -> None:
        before = self.overview.read_bytes()
        page = self.repository.get_page("acme-orbit-overview")
        invalid = replace(
            page,
            source_refs=("missing-source",),
            body=page.body.replace(
                "[[source:src-orbit-charter-v1]]", "[[source:missing-source]]"
            ),
        )
        with self.assertRaises(WikiValidationError):
            self.repository.write_page(invalid, section="projects")
        self.assertEqual(self.overview.read_bytes(), before)

    def test_concurrent_write_locking_uses_process_lock(self) -> None:
        context = get_context("fork")
        queue = context.Queue()
        with self.repository.locked():
            process = context.Process(target=_try_lock, args=(str(self.root), queue))
            process.start()
            process.join(3)
            self.assertEqual(queue.get(timeout=1), "timeout")
            self.assertEqual(process.exitcode, 0)
        self.assertFalse(self.repository.lock_state()["locked"])

    def test_concurrent_writers_do_not_corrupt_page(self) -> None:
        context = get_context("fork")
        queue = context.Queue()
        start = context.Event()
        processes = [
            context.Process(
                target=_write_title,
                args=(str(self.root), title, start, queue),
            )
            for title in ("Concurrent title A", "Concurrent title B")
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        results = sorted(queue.get(timeout=1) for _ in processes)
        self.assertEqual(results, ["ok", "ok"])
        self.assertIn(
            self.repository.get_page("acme-orbit-overview").title,
            {"Concurrent title A", "Concurrent title B"},
        )
        self.assertEqual(self.repository.validate(), [])

    def test_stale_lock_metadata_does_not_block(self) -> None:
        lock_dir = self.root / ".locks"
        lock_dir.mkdir(mode=0o700)
        (lock_dir / "repository.lock").write_text(
            '{"pid": 999999, "acquired_at": "2000-01-01T00:00:00Z"}',
            encoding="utf-8",
        )
        page = self.repository.get_page("acme-orbit-overview")
        self.repository.write_page(replace(page, title="After stale lock"), section="projects")
        self.assertEqual(self.repository.get_page(page.id).title, "After stale lock")

    def test_unexpected_existing_directory_during_init(self) -> None:
        root = self.base / "unknown"
        root.mkdir(mode=0o750)
        (root / "private.txt").write_text("unknown", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            WikiRepository(root).initialize()

    def test_clean_init_without_samples(self) -> None:
        root = self.base / "empty-wiki"
        repository = WikiRepository(root)
        result = repository.initialize()
        self.assertTrue(result["created"])
        self.assertEqual(repository.validate(), [])
        self.assertEqual(repository.status(self.base / "backups")["page_count"], 0)

    def test_deterministic_serialization(self) -> None:
        page = parse_page(self.overview.read_text(encoding="utf-8"))
        first = serialize_page(page)
        self.assertEqual(first, serialize_page(parse_page(first)))
        source = parse_source(self.source.read_text(encoding="utf-8"))
        self.assertEqual(serialize_source(source), serialize_source(parse_source(serialize_source(source))))

    def test_stable_validation_error_ordering(self) -> None:
        (self.root / "schema/UNKNOWN.md").write_text("unknown", encoding="utf-8")
        self.overview.write_text("bad", encoding="utf-8")
        first = [issue.as_dict() for issue in self.repository.validate()]
        second = [issue.as_dict() for issue in self.repository.validate()]
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            sorted(first, key=lambda item: (
                item["file"], item["code"], item["item_id"], item["field"], item["message"]
            )),
        )

    def test_git_repository_status_reporting(self) -> None:
        commit = self.git_init()
        status = self.repository.status(self.base / "backups")
        self.assertEqual(status["git"]["commit"], commit)
        self.assertEqual(status["git"]["branch"], "main")
        self.assertFalse(status["git"]["dirty"])
        (self.root / "README.md").write_text(
            (self.root / "README.md").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        self.assertTrue(self.repository.status(self.base / "backups")["git"]["dirty"])

    def test_backup_creation(self) -> None:
        commit = self.git_init()
        backup = self.repository.backup(self.base / "backups")
        self.assertEqual(S_IMODE(backup.stat().st_mode), 0o700)
        self.assertTrue((backup / "wiki.bundle").is_file())
        metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["source_commit"], commit)
        self.assertFalse(metadata["dirty"])

    def test_backup_checksum_verification(self) -> None:
        self.git_init()
        backup = self.repository.backup(self.base / "backups")
        for line in (backup / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((backup / name).read_bytes()).hexdigest(), digest)

    def test_restore_verification(self) -> None:
        commit = self.git_init()
        backup = self.repository.backup(self.base / "backups")
        result = WikiRepository.restore_verify(backup)
        self.assertTrue(result["verified"])
        self.assertEqual(result["source_commit"], commit)
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["source_manifest_count"], 3)

    def test_corrupt_backup_detection(self) -> None:
        self.git_init()
        backup = self.repository.backup(self.base / "backups")
        (backup / "metadata.json").write_text("corrupt\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum failed"):
            WikiRepository.restore_verify(backup)

    def test_restore_rejects_unsafe_working_file_metadata(self) -> None:
        self.git_init()
        backup = self.repository.backup(self.base / "backups")
        metadata_path = backup / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["working_files"] = ["../escape"]
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        sums_path = backup / "SHA256SUMS"
        sums_path.write_text(
            "".join(
                f"{hashlib.sha256((backup / name).read_bytes()).hexdigest()}  {name}\n"
                for name in (
                    "BACKUP-MANIFEST.md", "git-status.txt", "metadata.json",
                    "wiki.bundle", "working-tree.tar.gz",
                )
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Unsafe working-file metadata"):
            WikiRepository.restore_verify(backup)

    def test_empty_repository(self) -> None:
        root = self.base / "empty"
        root.mkdir(mode=0o750)
        issues = WikiRepository(root).validate()
        self.assertIn("missing_path", {issue.code for issue in issues})

    def test_unknown_extra_files(self) -> None:
        (self.root / "schema/UNKNOWN.md").write_text("not allowed\n", encoding="utf-8")
        self.assertIn("unknown_file", self.codes())

    def test_unicode_title_and_aliases(self) -> None:
        page = self.repository.get_page("acme-orbit-overview")
        revised = replace(page, title="Órbita – guía", aliases=("Översikt", "概要"))
        parsed = parse_page(serialize_page(revised))
        self.assertEqual(parsed.title, "Órbita – guía")
        self.assertEqual(parsed.aliases, ("Översikt", "概要"))

    def test_exact_safe_jira_key_normalization(self) -> None:
        self.assertEqual(normalize_jira_key(" orbit-42 "), "ORBIT-42")
        with self.assertRaises(ValueError):
            normalize_jira_key("../MH-1")
        page = self.repository.get_page("acme-orbit-overview")
        with self.assertRaisesRegex(WikiValidationError, "invalid_jira_key"):
            parse_page(serialize_page(replace(page, jira_keys=("orbit-42",))))

    def test_duplicate_alias_and_jira_key_rejected(self) -> None:
        page = self.repository.get_page("acme-orbit-overview")
        with self.assertRaisesRegex(WikiValidationError, "duplicate_value"):
            parse_page(serialize_page(replace(page, aliases=("Same", "same"))))
        with self.assertRaisesRegex(WikiValidationError, "duplicate_value"):
            parse_page(serialize_page(replace(page, jira_keys=("ORBIT-1", "ORBIT-1"))))

    def test_yaml_duplicate_keys_and_aliases_rejected(self) -> None:
        text = self.overview.read_text(encoding="utf-8")
        duplicate = text.replace(
            "id: acme-orbit-overview\n",
            "id: acme-orbit-overview\nid: duplicate\n",
        )
        with self.assertRaisesRegex(WikiValidationError, "malformed_yaml"):
            parse_page(duplicate)
        aliased = self.source.read_text(encoding="utf-8").replace(
            "title: Acme Orbit fictional charter",
            "title: &title Acme Orbit fictional charter",
        ).replace(
            "notes: Synthetic Phase 1.0 source fixture; no production content.",
            "notes: *title",
        )
        with self.assertRaisesRegex(WikiValidationError, "malformed_yaml"):
            parse_source(aliased)

    def test_absolute_paths_and_credential_locators_rejected(self) -> None:
        page = self.repository.get_page("acme-orbit-overview")
        with self.assertRaisesRegex(WikiValidationError, "absolute_path"):
            parse_page(serialize_page(replace(page, body=page.body + "\nSee /etc/example.\n")))
        source = parse_source(self.source.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(WikiValidationError, "unsafe_locator"):
            parse_source(serialize_source(replace(
                source, locator="https://user:pass@example.invalid/source"
            )))

    def test_checksum_mismatch_rejected(self) -> None:
        original = self.root / "sources/originals/orbit-charter-v1.txt"
        original.write_text("changed synthetic bytes\n", encoding="utf-8")
        self.assertIn("checksum_mismatch", self.codes())

    def test_dry_run_does_not_write(self) -> None:
        before = self.overview.read_bytes()
        page = self.repository.get_page("acme-orbit-overview")
        self.repository.write_page(
            replace(page, title="Dry-run title"), section="projects", dry_run=True
        )
        self.assertEqual(self.overview.read_bytes(), before)

    def test_dirty_backup_is_explicit_and_restorable(self) -> None:
        self.git_init()
        readme = self.root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nDirty fixture.\n", encoding="utf-8")
        backup = self.repository.backup(self.base / "backups")
        manifest = (backup / "BACKUP-MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn("WARNING", manifest)
        self.assertTrue(WikiRepository.restore_verify(backup)["dirty_backup"])

    def test_cli_wiki_init_does_not_require_openwebui_config(self) -> None:
        root = self.base / "cli-wiki"
        output = io.StringIO()
        with patch.dict(os.environ, {"OWUI_SWARM_WIKI_ROOT": str(root)}, clear=True):
            with patch("sys.stdout", output):
                self.assertEqual(main(["wiki", "init", "--with-samples"]), 0)
                self.assertEqual(main(["wiki", "validate"]), 0)
        self.assertIn('"created": true', output.getvalue())

    def test_packaged_template_inventory_includes_dotfiles(self) -> None:
        template = resources.files("swarm_router").joinpath("wiki_template")
        self.assertTrue(template.joinpath(".gitignore").is_file())
        self.assertTrue(template.joinpath("wiki/decisions/.gitkeep").is_file())
        self.assertEqual(
            template.joinpath("schema/VERSION").read_text(encoding="utf-8").strip(),
            SCHEMA_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
