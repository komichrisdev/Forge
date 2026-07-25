from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import json
import os
import sqlite3
import unittest

from swarm_router.cli import main
from swarm_router.wiki import WikiRepository, parse_page, serialize_page
from swarm_router.wiki_search import INDEX_SCHEMA_VERSION, WikiIndex, WikiSearchError, sqlite_supports_fts5


def populate_fixture_pages(repository: WikiRepository, count: int = 500) -> None:
    sections = ("projects", "features", "decisions", "systems", "research", "glossary")
    template = repository.get_page("acme-orbit-overview")
    for number in range(count):
        page_id = f"fixture-{number:04d}"
        section = sections[number % len(sections)]
        title = f"Synthetic fixture page {number:04d}"
        body = (
            f"# {title}\n\n"
            f"This fictional fixture documents orbit cache scenario {number:04d}. "
            f"[[source:src-orbit-charter-v1]]\n\n"
            f"Use [[page:acme-orbit-overview]] as the synthetic reference point.\n"
        )
        page = replace(
            template,
            id=page_id,
            title=title,
            aliases=(f"Fixture {number:04d}",),
            jira_keys=(f"ORBIT-{1000 + number}",),
            source_refs=("src-orbit-charter-v1",),
            source_updated_at="2026-01-02T12:00:00Z",
            ingested_at="2026-01-03T12:00:00Z",
            verification_status="verified",
            confidence=80 + (number % 20),
            tags=("fixture", f"group-{number % 10}"),
            supersedes=(),
            body=body,
        )
        path = repository.root / "wiki" / section / f"{page_id}.md"
        path.write_text(serialize_page(page), encoding="utf-8")
    repository.require_valid()


class WikiSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "wiki"
        self.repository = WikiRepository(self.root)
        self.repository.initialize(with_samples=True)
        self.index = WikiIndex(self.repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli_json(self, *args: str) -> dict[str, object]:
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = main(["wiki", "--root", str(self.root), *args])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def page_path(self, page_id: str) -> Path:
        matches = list((self.root / "wiki").glob(f"*/{page_id}.md"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def write_direct(self, page_id: str, *, section: str | None = None, **changes: object) -> None:
        path = self.page_path(page_id)
        page = parse_page(path.read_text(encoding="utf-8"))
        if section is None:
            section = path.parent.name
        path.unlink()
        revised = replace(page, **changes)
        target = self.root / "wiki" / section / f"{revised.id}.md"
        target.write_text(serialize_page(revised), encoding="utf-8")
        self.repository.require_valid()

    def test_sqlite_fts5_is_available(self) -> None:
        self.assertTrue(sqlite_supports_fts5())

    def test_empty_wiki_indexes_cleanly(self) -> None:
        empty_root = Path(self.temporary.name) / "empty"
        repository = WikiRepository(empty_root)
        repository.initialize()
        index = WikiIndex(repository)
        result = index.build(full=True)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["page_count"], 0)
        self.assertEqual(index.search("anything")["results"], [])

    def test_search_covers_page_id_alias_jira_tag_phrase_and_body(self) -> None:
        self.index.build(full=True)
        self.assertEqual(
            self.index.search("acme-orbit-overview")["results"][0]["page_id"],
            "acme-orbit-overview",
        )
        self.assertEqual(
            self.index.search("Orbit training project")["results"][0]["page_id"],
            "acme-orbit-overview",
        )
        self.assertEqual(
            self.index.search("ORBIT-7")["results"][0]["page_id"],
            "acme-orbit-cache-decision",
        )
        self.assertEqual(
            self.index.search("cache")["results"][0]["page_id"],
            "acme-orbit-cache-decision",
        )
        self.assertEqual(
            self.index.search("\"five-minute in-memory cache\"")["results"][0]["page_id"],
            "acme-orbit-cache-decision",
        )
        self.assertEqual(
            self.index.search("synthetic process cache")["results"][0]["page_id"],
            "acme-orbit-recovery-runbook",
        )

    def test_exact_match_ranks_before_lexical_match(self) -> None:
        page = self.repository.get_page("acme-orbit-cache-decision")
        distractor = replace(
            page,
            id="cache-note",
            title="Cache note",
            aliases=("Overview mention",),
            jira_keys=("ORBIT-99",),
            tags=("cache", "synthetic"),
            supersedes=(),
            body=(
                "# Cache note\n\n"
                "This fictional note repeats acme orbit overview text for lexical matching. "
                "[[source:src-orbit-cache-decision-v1]]\n"
            ),
        )
        self.repository.write_page(distractor, section="features")
        self.index.build(full=True)
        results = self.index.search("acme-orbit-overview")["results"]
        self.assertEqual(results[0]["page_id"], "acme-orbit-overview")
        self.assertIn("cache-note", {item["page_id"] for item in results})

    def test_deterministic_ordering_uses_page_id_tiebreak(self) -> None:
        page = self.repository.get_page("acme-orbit-overview")
        for page_id in ("alpha-shared", "beta-shared"):
            self.repository.write_page(replace(
                page,
                id=page_id,
                title="Shared synthetic page",
                aliases=("shared-token",),
                jira_keys=(),
                tags=("shared",),
                supersedes=(),
                body="# Shared\n\nsharedtoken [[source:src-orbit-charter-v1]]\n",
            ), section="research")
        self.index.build(full=True)
        results = [item["page_id"] for item in self.index.search("sharedtoken")["results"][:2]]
        self.assertEqual(results, ["alpha-shared", "beta-shared"])

    def test_filters_and_stale_queries_work(self) -> None:
        self.index.build(full=True)
        filtered = self.index.search(
            "synthetic",
            verification="verified",
            min_confidence=90,
            jira_key="ORBIT-1",
        )
        self.assertEqual([item["page_id"] for item in filtered["results"]], ["acme-orbit-overview"])
        stale = self.index.stale(days=1)
        self.assertGreaterEqual(stale["result_count"], 3)

    def test_superseded_pages_remain_visible_but_current_pages_rank_first(self) -> None:
        path = self.page_path("acme-orbit-overview")
        page = parse_page(path.read_text(encoding="utf-8"))
        path.write_text(
            serialize_page(replace(
                page,
                verification_status="superseded",
                supersedes=(),
            )),
            encoding="utf-8",
        )
        replacement = replace(
            page,
            id="acme-orbit-overview-v2",
            title="Acme Orbit overview",
            jira_keys=("ORBIT-101",),
            supersedes=("acme-orbit-overview",),
            body=(
                "# Acme Orbit\n\n"
                "Synthetic replacement overview. [[source:src-orbit-charter-v1]]\n"
            ),
        )
        replacement_path = self.root / "wiki" / "projects" / "acme-orbit-overview-v2.md"
        replacement_path.write_text(serialize_page(replacement), encoding="utf-8")
        self.repository.require_valid()
        self.index.build(full=True)
        results = self.index.search("Acme Orbit overview")["results"]
        self.assertEqual(results[0]["page_id"], "acme-orbit-overview-v2")
        self.assertIn("acme-orbit-overview", [item["page_id"] for item in results])
        self.assertFalse([item for item in results if item["page_id"] == "acme-orbit-overview"][0]["current"])

    def test_invalid_fts_query_is_clean(self) -> None:
        self.index.build(full=True)
        with self.assertRaisesRegex(ValueError, "Invalid FTS query"):
            self.index.search("\"unterminated")

    def test_unicode_search_matches_aliases(self) -> None:
        self.index.build(full=True)
        result = self.index.search("Órbita de ejemplo")
        self.assertEqual(result["results"][0]["page_id"], "acme-orbit-overview")

    def test_incremental_index_handles_add_edit_move_and_delete(self) -> None:
        self.index.build(full=True)
        overview = self.repository.get_page("acme-orbit-overview")
        self.repository.write_page(replace(
            overview,
            id="acme-orbit-planning-note",
            title="Acme Orbit planning note",
            aliases=("Orbit planning note",),
            jira_keys=("ORBIT-88",),
            tags=("planning", "synthetic"),
            supersedes=(),
            body="# Planning\n\nSynthetic planning note. [[source:src-orbit-charter-v1]]\n",
        ), section="research")
        decision_path = self.page_path("acme-orbit-cache-decision")
        decision = parse_page(decision_path.read_text(encoding="utf-8"))
        decision_path.write_text(
            serialize_page(replace(decision, title="Acme Orbit cache decision revised")),
            encoding="utf-8",
        )
        runbook_source = self.page_path("acme-orbit-recovery-runbook")
        runbook_target = self.root / "wiki" / "research" / runbook_source.name
        runbook_source.rename(runbook_target)
        self.repository.require_valid()
        update = self.index.build()
        self.assertEqual(update["mode"], "incremental")
        self.assertEqual(update["added"], 1)
        self.assertEqual(update["updated"], 2)
        self.assertEqual(update["deleted"], 0)
        self.assertEqual(
            self.index.search("planning")["results"][0]["page_id"],
            "acme-orbit-planning-note",
        )
        (self.root / "wiki" / "research" / "acme-orbit-planning-note.md").unlink()
        self.repository.require_valid()
        deleted = self.index.build()
        self.assertEqual(deleted["deleted"], 1)
        self.assertEqual(self.index.search("planning")["results"], [])

    def test_schema_mismatch_forces_full_rebuild(self) -> None:
        self.index.build(full=True)
        connection = sqlite3.connect(self.index.path)
        try:
            connection.execute("UPDATE index_metadata SET schema_version = 0 WHERE singleton = 1")
            connection.commit()
        finally:
            connection.close()
        result = self.index.build()
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["fallback_reason"], "schema-mismatch")

    def test_corrupt_database_forces_full_rebuild(self) -> None:
        self.index.build(full=True)
        self.index.path.write_text("not sqlite\n", encoding="utf-8")
        result = self.index.build()
        self.assertEqual(result["mode"], "full")
        self.assertTrue(result["fallback_reason"])

    def test_replace_failure_leaves_previous_index_unchanged(self) -> None:
        self.index.build(full=True)
        before = self.index.path.read_bytes()
        self.write_direct("acme-orbit-overview", title="Changed after failed replace")
        with patch("swarm_router.wiki_search.os.replace", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                self.index.build(full=True)
        self.assertEqual(self.index.path.read_bytes(), before)
        self.assertEqual(list(self.index.path.parent.glob(".*.tmp")), [])

    def test_existing_readers_survive_atomic_rebuild(self) -> None:
        self.index.build(full=True)
        reader = sqlite3.connect(self.index.path)
        try:
            initial = reader.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            self.write_direct("acme-orbit-overview", title="Concurrent rebuild title")
            self.index.build()
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM pages").fetchone()[0], initial)
        finally:
            reader.close()
        self.assertEqual(
            self.index.search("Concurrent rebuild title")["results"][0]["page_id"],
            "acme-orbit-overview",
        )

    def test_indexing_does_not_modify_source_manifests(self) -> None:
        manifest = self.root / "sources" / "manifests" / "src-orbit-charter-v1.yaml"
        before = manifest.read_bytes()
        self.index.build(full=True)
        self.assertEqual(manifest.read_bytes(), before)

    def test_traversal_like_related_id_is_rejected(self) -> None:
        self.index.build(full=True)
        with self.assertRaisesRegex(ValueError, "Invalid page ID"):
            self.index.related("../escape")

    def test_cli_json_commands_and_status_drift(self) -> None:
        index_payload = self.cli_json("index", "--full")
        self.assertEqual(index_payload["mode"], "full")
        search_payload = self.cli_json("search", "ORBIT-7")
        self.assertEqual(search_payload["results"][0]["page_id"], "acme-orbit-cache-decision")
        related_payload = self.cli_json("related", "acme-orbit-overview")
        self.assertGreaterEqual(related_payload["result_count"], 1)
        stale_payload = self.cli_json("stale", "--days", "1")
        self.assertGreaterEqual(stale_payload["result_count"], 1)
        self.write_direct("acme-orbit-overview", title="Drifted title")
        status_payload = self.cli_json("status")
        self.assertEqual(status_payload["index"]["freshness"], "stale")
        self.assertEqual(status_payload["index"]["drift"]["changed"], 1)

    def test_search_refuses_stale_index(self) -> None:
        self.index.build(full=True)
        self.write_direct("acme-orbit-overview", title="Drifted title")
        with self.assertRaises(WikiSearchError):
            self.index.search("Drifted title")

    def test_benchmark_fixture_population_is_valid(self) -> None:
        populate_fixture_pages(self.repository, count=25)
        self.index.build(full=True)
        self.assertEqual(self.index.status()["indexed_page_count"], 28)
        self.assertEqual(self.index.search("fixture 0007")["results"][0]["page_id"], "fixture-0007")


if __name__ == "__main__":
    unittest.main()
