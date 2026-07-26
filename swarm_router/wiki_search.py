from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from shutil import copy2
from typing import Any, Iterator
import json
import os
import sqlite3

from .wiki import (
    PAGE_SECTIONS,
    VERIFICATION_STATUSES,
    ID_RE,
    WikiPage,
    WikiRepository,
    normalize_jira_key,
    parse_page,
)


INDEX_FILENAME = "wiki.db"
INDEX_SCHEMA_VERSION = 1
INDEX_TIMEOUT_SECONDS = 5.0
SEARCH_LIMIT = 20
RELATED_LIMIT = 10
SNIPPET_TOKENS = 14


class WikiSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class PageRecord:
    page: WikiPage
    canonical_path: str
    content_hash: str
    slug: str
    aliases_text: str
    jira_keys_text: str
    tags_text: str
    source_refs_text: str
    body_text: str
    superseded_by: tuple[str, ...]
    is_current: int

    def metadata_row(self) -> tuple[Any, ...]:
        return (
            self.page.id,
            self.slug,
            self.page.title,
            self.page.project,
            self.page.verification_status,
            self.page.confidence,
            self.page.source_updated_at,
            self.page.ingested_at,
            self.canonical_path,
            self.content_hash,
            _json_list(self.page.aliases),
            _json_list(self.page.jira_keys),
            _json_list(self.page.source_refs),
            _json_list(self.page.tags),
            _json_list(self.page.supersedes),
            _json_list(self.superseded_by),
            self.is_current,
            INDEX_SCHEMA_VERSION,
        )

    def fts_row(self) -> tuple[str, ...]:
        return (
            self.page.id,
            self.slug,
            self.page.title,
            self.aliases_text,
            self.jira_keys_text,
            self.tags_text,
            self.source_refs_text,
            self.body_text,
        )


@dataclass(frozen=True)
class WikiSnapshot:
    built_at: str
    source_commit: str | None
    source_branch: str | None
    source_manifest_count: int
    filesystem_token: int
    records: tuple[PageRecord, ...]


class _NeedsFullRebuild(RuntimeError):
    pass


def sqlite_supports_fts5() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compile_match_query(query: str) -> str:
    stripped = query.strip()
    if not stripped:
        raise ValueError("Search query must not be empty")
    if any(char in stripped for char in "\"()*"):
        return stripped
    return " ".join(f"\"{token.replace('\"', '\"\"')}\"" for token in stripped.split())


def _exact_page_id(query: str) -> str | None:
    candidate = query.strip().strip("\"")
    if ID_RE.fullmatch(candidate):
        return candidate
    return None


def _exact_jira_key(query: str) -> str | None:
    candidate = query.strip().strip("\"")
    if not candidate or any(char.isspace() for char in candidate):
        return None
    try:
        return normalize_jira_key(candidate)
    except ValueError:
        return None


def _result_lists(row: sqlite3.Row) -> dict[str, list[str]]:
    return {
        "aliases": json.loads(row["aliases_json"]),
        "jira_keys": json.loads(row["jira_keys_json"]),
        "source_refs": json.loads(row["source_refs_json"]),
        "tags": json.loads(row["tags_json"]),
        "supersedes": json.loads(row["supersedes_json"]),
        "superseded_by": json.loads(row["superseded_by_json"]),
    }


class WikiIndex:
    def __init__(self, repository: WikiRepository | str | Path | None = None):
        if isinstance(repository, WikiRepository):
            self.repository = repository
        else:
            self.repository = WikiRepository(repository)
        self.path = self.repository.root / "index" / INDEX_FILENAME

    def build(self, *, full: bool = False) -> dict[str, Any]:
        if not sqlite_supports_fts5():
            raise WikiSearchError("sqlite3 on this host does not support FTS5")
        self.repository._assert_root()
        self.repository._safe_path(self.path.parent)
        built_at = _utc_now()
        with self.repository.locked():
            snapshot = self._snapshot(built_at)
            target = self.repository._safe_path(self.path)
            target.parent.mkdir(exist_ok=True, mode=0o750)
            if target.exists() and target.is_symlink():
                raise WikiSearchError("Index path must not be a symlink")
            mode = "full" if full or not target.exists() else "incremental"
            fallback_reason: str | None = None
            temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                if mode == "incremental":
                    # ponytail: copy the whole DB before patching it so readers keep the old inode;
                    # switch to a shadow-build strategy only if index size makes this too slow.
                    copy2(target, temp_path)
                    try:
                        stats = self._incremental_build(temp_path, snapshot)
                    except (_NeedsFullRebuild, sqlite3.DatabaseError, sqlite3.OperationalError, WikiSearchError) as exc:
                        fallback_reason = str(exc)
                        mode = "full"
                if mode == "full":
                    self._unlink_if_exists(temp_path)
                    stats = self._full_build(temp_path, snapshot)
                self._verify_database(temp_path, snapshot, run_queries=True)
                self._promote_database(temp_path, target)
            finally:
                self._unlink_if_exists(temp_path)
        return {
            "path": str(target),
            "schema_version": INDEX_SCHEMA_VERSION,
            "mode": mode,
            "fallback_reason": fallback_reason,
            "page_count": len(snapshot.records),
            "source_manifest_count": snapshot.source_manifest_count,
            "indexed_page_count": len(snapshot.records),
            "last_build": built_at,
            **stats,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = SEARCH_LIMIT,
        verification: str | None = None,
        min_confidence: int | None = None,
        jira_key: str | None = None,
    ) -> dict[str, Any]:
        self._require_current_index()
        if limit < 1:
            raise ValueError("Search limit must be at least 1")
        if verification is not None and verification not in VERIFICATION_STATUSES:
            raise ValueError(f"Verification must be one of: {', '.join(VERIFICATION_STATUSES)}")
        match_query = _compile_match_query(query)
        exact_page_id = _exact_page_id(query)
        exact_jira = normalize_jira_key(jira_key) if jira_key else _exact_jira_key(query)
        parameters: list[Any] = [exact_page_id, exact_page_id, exact_jira, exact_jira, match_query]
        filters: list[str] = []
        if verification is not None:
            filters.append("pages.verification_status = ?")
            parameters.append(verification)
        if min_confidence is not None:
            if min_confidence < 0 or min_confidence > 100:
                raise ValueError("Minimum confidence must be between 0 and 100")
            filters.append("pages.confidence >= ?")
            parameters.append(min_confidence)
        if jira_key is not None:
            filters.append(
                "EXISTS (SELECT 1 FROM page_jira_keys WHERE page_jira_keys.page_id = pages.page_id AND page_jira_keys.jira_key = ?)"
            )
            parameters.append(normalize_jira_key(jira_key))
        where_sql = ""
        if filters:
            where_sql = " AND " + " AND ".join(filters)
        try:
            with self._open_index("ro") as connection:
                rows = connection.execute(
                    f"""
                    SELECT
                        pages.page_id,
                        pages.title,
                        pages.verification_status,
                        pages.confidence,
                        pages.canonical_path,
                        pages.aliases_json,
                        pages.jira_keys_json,
                        pages.source_refs_json,
                        pages.tags_json,
                        pages.supersedes_json,
                        pages.superseded_by_json,
                        pages.is_current,
                        COALESCE(NULLIF(snippet(page_search, 7, '[', ']', ' … ', {SNIPPET_TOKENS}), ''), pages.title) AS snippet,
                        CASE
                            WHEN pages.page_id = ? THEN 3
                            WHEN pages.slug = ? THEN 2
                            WHEN ? IS NOT NULL AND EXISTS (
                                SELECT 1 FROM page_jira_keys
                                WHERE page_jira_keys.page_id = pages.page_id AND page_jira_keys.jira_key = ?
                            ) THEN 2
                            ELSE 0
                        END AS exact_rank,
                        bm25(page_search, 25.0, 20.0, 18.0, 12.0, 10.0, 8.0, 8.0, 1.0) AS bm25_score
                    FROM page_search
                    JOIN pages ON pages.page_id = page_search.page_id
                    WHERE page_search MATCH ?{where_sql}
                    ORDER BY exact_rank DESC, pages.is_current DESC, bm25_score ASC, pages.page_id ASC
                    LIMIT ?
                    """,
                    [*parameters, limit],
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Invalid FTS query: {exc}") from exc
        results = []
        for row in rows:
            score = (row["exact_rank"] * 1_000_000.0) - float(row["bm25_score"])
            lists = _result_lists(row)
            results.append({
                "page_id": row["page_id"],
                "title": row["title"],
                "snippet": row["snippet"],
                "score": round(score, 6),
                "verification": row["verification_status"],
                "confidence": row["confidence"],
                "jira_keys": lists["jira_keys"],
                "tags": lists["tags"],
                "sources": lists["source_refs"],
                "canonical_path": row["canonical_path"],
                "current": bool(row["is_current"]),
            })
        return {
            "query": query,
            "match_query": match_query,
            "result_count": len(results),
            "results": results,
        }

    def related(self, page_id: str, *, limit: int = RELATED_LIMIT) -> dict[str, Any]:
        target = _exact_page_id(page_id)
        if target is None:
            raise ValueError(f"Invalid page ID: {page_id!r}")
        if limit < 1:
            raise ValueError("Related result limit must be at least 1")
        self._require_current_index()
        with self._open_index("ro") as connection:
            base = connection.execute("SELECT * FROM pages WHERE page_id = ?", (target,)).fetchone()
            if base is None:
                raise KeyError(f"Page not found: {page_id}")
            base_lists = _result_lists(base)
            rows = connection.execute(
                "SELECT * FROM pages WHERE page_id != ? ORDER BY page_id ASC",
                (target,),
            ).fetchall()
        results = []
        for row in rows:
            lists = _result_lists(row)
            score = 0
            if row["project"] == base["project"]:
                score += 3
            score += 2 * len(set(lists["tags"]) & set(base_lists["tags"]))
            score += 4 * len(set(lists["source_refs"]) & set(base_lists["source_refs"]))
            score += 3 * len(set(lists["jira_keys"]) & set(base_lists["jira_keys"]))
            if target in lists["supersedes"] or row["page_id"] in base_lists["supersedes"]:
                score += 5
            if score == 0:
                continue
            results.append({
                "page_id": row["page_id"],
                "title": row["title"],
                "score": score,
                "verification": row["verification_status"],
                "confidence": row["confidence"],
                "jira_keys": lists["jira_keys"],
                "sources": lists["source_refs"],
                "canonical_path": row["canonical_path"],
                "current": bool(row["is_current"]),
            })
        results.sort(key=lambda item: (-item["score"], not item["current"], item["page_id"]))
        return {"page_id": target, "result_count": min(len(results), limit), "results": results[:limit]}

    def stale(self, *, days: int) -> dict[str, Any]:
        if days < 0:
            raise ValueError("Days must be zero or greater")
        self._require_current_index()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        results = []
        with self._open_index("ro") as connection:
            rows = connection.execute(
                "SELECT * FROM pages ORDER BY source_updated_at ASC, page_id ASC"
            ).fetchall()
        for row in rows:
            updated = datetime.fromisoformat(row["source_updated_at"].replace("Z", "+00:00"))
            if updated > cutoff:
                continue
            lists = _result_lists(row)
            age_days = int((now - updated).total_seconds() // 86400)
            results.append({
                "page_id": row["page_id"],
                "title": row["title"],
                "source_updated_at": row["source_updated_at"],
                "ingested_at": row["ingested_at"],
                "age_days": age_days,
                "verification": row["verification_status"],
                "confidence": row["confidence"],
                "jira_keys": lists["jira_keys"],
                "sources": lists["source_refs"],
                "canonical_path": row["canonical_path"],
                "current": bool(row["is_current"]),
            })
        return {"days": days, "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"), "result_count": len(results), "results": results}

    def status(self) -> dict[str, Any]:
        current: dict[str, tuple[str, str]] = {}
        page_count = 0
        validation = "invalid"
        try:
            snapshot = self._snapshot(_utc_now())
            current = {record.page.id: (record.content_hash, record.canonical_path) for record in snapshot.records}
            page_count = len(snapshot.records)
            validation = "valid"
        except Exception:
            snapshot = None
        info: dict[str, Any] = {
            "present": self.path.is_file(),
            "path": str(self.path),
            "schema_version": None,
            "canonical_page_count": page_count,
            "indexed_page_count": 0,
            "last_build": None,
            "freshness": "missing",
            "drift": {"added": 0, "changed": 0, "removed": 0},
            "validation": validation,
        }
        if not self.path.exists():
            return info
        try:
            with self._open_index("ro") as connection:
                metadata = connection.execute(
                    "SELECT schema_version, built_at, page_count, filesystem_token FROM index_metadata WHERE singleton = 1"
                ).fetchone()
                indexed_rows = connection.execute(
                    "SELECT page_id, content_hash, canonical_path FROM pages ORDER BY page_id ASC"
                ).fetchall()
                self._check_connection(connection, require_fts_check=False)
        except (sqlite3.DatabaseError, sqlite3.OperationalError, WikiSearchError):
            info["freshness"] = "invalid"
            return info
        if metadata is None:
            info["freshness"] = "invalid"
            return info
        indexed = {row["page_id"]: (row["content_hash"], row["canonical_path"]) for row in indexed_rows}
        added = sorted(set(current) - set(indexed))
        removed = sorted(set(indexed) - set(current))
        changed = sorted(
            page_id for page_id in set(current) & set(indexed)
            if current[page_id] != indexed[page_id]
        )
        info.update({
            "schema_version": metadata["schema_version"],
            "indexed_page_count": metadata["page_count"],
            "last_build": metadata["built_at"],
            "drift": {
                "added": len(added),
                "changed": len(changed),
                "removed": len(removed),
            },
        })
        if metadata["schema_version"] != INDEX_SCHEMA_VERSION:
            info["freshness"] = "stale"
        elif validation != "valid":
            info["freshness"] = "invalid"
        elif not added and not changed and not removed and page_count == metadata["page_count"]:
            info["freshness"] = "current"
        else:
            info["freshness"] = "stale"
        return info

    def _require_current_index(self) -> None:
        if not self.path.is_file():
            raise WikiSearchError("Wiki index is missing or stale; run `owui-swarm wiki index`")
        current_token = self._filesystem_token()
        with self._open_index("ro") as connection:
            metadata = connection.execute(
                "SELECT schema_version, filesystem_token FROM index_metadata WHERE singleton = 1"
            ).fetchone()
            self._check_connection(connection, require_fts_check=False)
        if metadata is None or metadata["schema_version"] != INDEX_SCHEMA_VERSION:
            raise WikiSearchError("Wiki index is missing or stale; run `owui-swarm wiki index`")
        if current_token != metadata["filesystem_token"]:
            raise WikiSearchError("Wiki index is missing or stale; run `owui-swarm wiki index`")

    def _snapshot(self, built_at: str) -> WikiSnapshot:
        self.repository.require_valid()
        git = self.repository.git_info()
        parsed_pages: list[tuple[Path, WikiPage, str]] = []
        superseded_by: dict[str, list[str]] = {}
        for section in PAGE_SECTIONS:
            for path in sorted((self.repository.root / "wiki" / section).glob("*.md")):
                relative = path.relative_to(self.repository.root).as_posix()
                text = path.read_text(encoding="utf-8")
                page = parse_page(text, relative)
                parsed_pages.append((path, page, text))
                for target in page.supersedes:
                    superseded_by.setdefault(target, []).append(page.id)
        records = []
        for path, page, text in parsed_pages:
            records.append(PageRecord(
                page=page,
                canonical_path=path.relative_to(self.repository.root).as_posix(),
                content_hash=sha256(text.encode("utf-8")).hexdigest(),
                slug=page.id,
                aliases_text=" ".join(page.aliases),
                jira_keys_text=" ".join(page.jira_keys),
                tags_text=" ".join(page.tags),
                source_refs_text=" ".join(page.source_refs),
                body_text=page.body,
                superseded_by=tuple(sorted(superseded_by.get(page.id, []))),
                is_current=0 if page.verification_status == "superseded" else 1,
            ))
        manifest_count = len(list((self.repository.root / "sources" / "manifests").glob("*.yaml")))
        return WikiSnapshot(
            built_at=built_at,
            source_commit=git["commit"],
            source_branch=git["branch"],
            source_manifest_count=manifest_count,
            filesystem_token=self._filesystem_token(),
            records=tuple(sorted(records, key=lambda item: item.page.id)),
        )

    def _filesystem_token(self) -> int:
        # ponytail: mtime token keeps search fast; use content hashing here only if
        # operators start preserving mtimes while editing canonical files.
        token = 0
        for base in (
            self.repository.root / "wiki",
            self.repository.root / "sources" / "manifests",
        ):
            if not base.exists():
                continue
            token = max(token, base.stat().st_mtime_ns)
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.is_dir():
                    token = max(token, path.stat().st_mtime_ns)
                elif path.is_file():
                    token = max(token, path.stat().st_mtime_ns)
        return token

    def _full_build(self, path: Path, snapshot: WikiSnapshot) -> dict[str, Any]:
        with self._open_index("rw", path=path, create=True) as connection:
            self._initialize_schema(connection, snapshot)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for record in snapshot.records:
                    self._insert_record(connection, record)
                self._update_metadata(connection, snapshot, "full")
                self._check_connection(connection, require_fts_check=True)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "added": len(snapshot.records),
            "updated": 0,
            "deleted": 0,
            "unchanged": 0,
        }

    def _incremental_build(self, path: Path, snapshot: WikiSnapshot) -> dict[str, Any]:
        with self._open_index("rw", path=path) as connection:
            self._check_connection(connection, require_fts_check=True)
            connection.commit()
            metadata = connection.execute(
                "SELECT schema_version FROM index_metadata WHERE singleton = 1"
            ).fetchone()
            if metadata is None or metadata["schema_version"] != INDEX_SCHEMA_VERSION:
                raise _NeedsFullRebuild("schema-mismatch")
            existing = {
                row["page_id"]: (row["content_hash"], row["canonical_path"])
                for row in connection.execute(
                    "SELECT page_id, content_hash, canonical_path FROM pages ORDER BY page_id ASC"
                )
            }
            current = {record.page.id: record for record in snapshot.records}
            added_ids = sorted(set(current) - set(existing))
            deleted_ids = sorted(set(existing) - set(current))
            updated_ids = sorted(
                page_id for page_id in set(current) & set(existing)
                if existing[page_id] != (current[page_id].content_hash, current[page_id].canonical_path)
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for page_id in deleted_ids:
                    self._delete_record(connection, page_id)
                for page_id in [*updated_ids, *added_ids]:
                    self._delete_record(connection, page_id)
                    self._insert_record(connection, current[page_id])
                self._update_metadata(connection, snapshot, "incremental")
                self._check_connection(connection, require_fts_check=True)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "added": len(added_ids),
            "updated": len(updated_ids),
            "deleted": len(deleted_ids),
            "unchanged": max(len(snapshot.records) - len(added_ids) - len(updated_ids), 0),
        }

    def _initialize_schema(self, connection: sqlite3.Connection, snapshot: WikiSnapshot) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=OFF;
            PRAGMA temp_store=MEMORY;
            PRAGMA user_version=1;
            DROP TABLE IF EXISTS index_metadata;
            DROP TABLE IF EXISTS pages;
            DROP TABLE IF EXISTS page_aliases;
            DROP TABLE IF EXISTS page_jira_keys;
            DROP TABLE IF EXISTS page_tags;
            DROP TABLE IF EXISTS page_source_refs;
            DROP TABLE IF EXISTS page_supersedes;
            DROP TABLE IF EXISTS page_search;
            CREATE TABLE index_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                built_at TEXT NOT NULL,
                build_mode TEXT NOT NULL,
                source_commit TEXT,
                source_branch TEXT,
                page_count INTEGER NOT NULL,
                source_manifest_count INTEGER NOT NULL,
                filesystem_token INTEGER NOT NULL
            );
            CREATE TABLE pages (
                page_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                project TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                source_updated_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                canonical_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                jira_keys_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                supersedes_json TEXT NOT NULL,
                superseded_by_json TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                index_version INTEGER NOT NULL
            );
            CREATE TABLE page_aliases (
                page_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (page_id, alias)
            ) WITHOUT ROWID;
            CREATE TABLE page_jira_keys (
                page_id TEXT NOT NULL,
                jira_key TEXT NOT NULL,
                PRIMARY KEY (page_id, jira_key)
            ) WITHOUT ROWID;
            CREATE TABLE page_tags (
                page_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (page_id, tag)
            ) WITHOUT ROWID;
            CREATE TABLE page_source_refs (
                page_id TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                PRIMARY KEY (page_id, source_ref)
            ) WITHOUT ROWID;
            CREATE TABLE page_supersedes (
                page_id TEXT NOT NULL,
                supersedes_page_id TEXT NOT NULL,
                PRIMARY KEY (page_id, supersedes_page_id)
            ) WITHOUT ROWID;
            CREATE INDEX pages_project_idx ON pages(project);
            CREATE INDEX pages_verification_idx ON pages(verification_status);
            CREATE INDEX pages_confidence_idx ON pages(confidence);
            CREATE VIRTUAL TABLE page_search USING fts5(
                page_id,
                slug,
                title,
                aliases,
                jira_keys,
                tags,
                source_refs,
                body
            );
            """
        )

    def _update_metadata(self, connection: sqlite3.Connection, snapshot: WikiSnapshot, build_mode: str) -> None:
        connection.execute("DELETE FROM index_metadata")
        connection.execute(
            """
            INSERT INTO index_metadata (
                singleton, schema_version, built_at, build_mode, source_commit, source_branch, page_count, source_manifest_count, filesystem_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                INDEX_SCHEMA_VERSION,
                snapshot.built_at,
                build_mode,
                snapshot.source_commit,
                snapshot.source_branch,
                len(snapshot.records),
                snapshot.source_manifest_count,
                snapshot.filesystem_token,
            ),
        )

    def _insert_record(self, connection: sqlite3.Connection, record: PageRecord) -> None:
        connection.execute(
            """
            INSERT INTO pages (
                page_id, slug, title, project, verification_status, confidence,
                source_updated_at, ingested_at, canonical_path, content_hash,
                aliases_json, jira_keys_json, source_refs_json, tags_json,
                supersedes_json, superseded_by_json, is_current, index_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            record.metadata_row(),
        )
        connection.execute(
            """
            INSERT INTO page_search (
                page_id, slug, title, aliases, jira_keys, tags, source_refs, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            record.fts_row(),
        )
        for alias in record.page.aliases:
            connection.execute(
                "INSERT INTO page_aliases (page_id, alias) VALUES (?, ?)",
                (record.page.id, alias),
            )
        for jira_key in record.page.jira_keys:
            connection.execute(
                "INSERT INTO page_jira_keys (page_id, jira_key) VALUES (?, ?)",
                (record.page.id, jira_key),
            )
        for tag in record.page.tags:
            connection.execute(
                "INSERT INTO page_tags (page_id, tag) VALUES (?, ?)",
                (record.page.id, tag),
            )
        for source_ref in record.page.source_refs:
            connection.execute(
                "INSERT INTO page_source_refs (page_id, source_ref) VALUES (?, ?)",
                (record.page.id, source_ref),
            )
        for target in record.page.supersedes:
            connection.execute(
                "INSERT INTO page_supersedes (page_id, supersedes_page_id) VALUES (?, ?)",
                (record.page.id, target),
            )

    def _delete_record(self, connection: sqlite3.Connection, page_id: str) -> None:
        connection.execute("DELETE FROM page_aliases WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM page_jira_keys WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM page_tags WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM page_source_refs WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM page_supersedes WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM page_search WHERE page_id = ?", (page_id,))
        connection.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))

    def _check_connection(self, connection: sqlite3.Connection, *, require_fts_check: bool) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise WikiSearchError("SQLite integrity check failed")
        if require_fts_check:
            connection.execute("INSERT INTO page_search(page_search) VALUES('integrity-check')")

    def _verify_database(self, path: Path, snapshot: WikiSnapshot, *, run_queries: bool) -> None:
        with self._open_index("rw", path=path) as connection:
            self._check_connection(connection, require_fts_check=True)
            metadata = connection.execute(
                "SELECT schema_version, page_count, source_manifest_count FROM index_metadata WHERE singleton = 1"
            ).fetchone()
            if metadata is None:
                raise WikiSearchError("Index metadata is missing")
            if metadata["schema_version"] != INDEX_SCHEMA_VERSION:
                raise WikiSearchError("Index schema version mismatch")
            if metadata["page_count"] != len(snapshot.records):
                raise WikiSearchError("Indexed page count does not match canonical page count")
            if metadata["source_manifest_count"] != snapshot.source_manifest_count:
                raise WikiSearchError("Indexed source count does not match canonical source count")
            indexed = {
                row["page_id"]: (row["content_hash"], row["canonical_path"])
                for row in connection.execute("SELECT page_id, content_hash, canonical_path FROM pages ORDER BY page_id ASC")
            }
            current = {record.page.id: (record.content_hash, record.canonical_path) for record in snapshot.records}
            if indexed != current:
                raise WikiSearchError("Indexed content hash set does not match canonical content")
            if run_queries and snapshot.records:
                record = snapshot.records[0]
                match_query = _compile_match_query(record.page.id)
                row = connection.execute(
                    """
                    SELECT pages.page_id
                    FROM page_search
                    JOIN pages ON pages.page_id = page_search.page_id
                    WHERE page_search MATCH ?
                    ORDER BY bm25(page_search), pages.page_id ASC
                    LIMIT 1
                    """,
                    (match_query,),
                ).fetchone()
                if row is None:
                    raise WikiSearchError("Representative FTS query returned no rows")

    @contextmanager
    def _open_index(
        self,
        mode: str,
        *,
        path: Path | None = None,
        create: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        target = path or self.path
        if create:
            connection = sqlite3.connect(target, timeout=INDEX_TIMEOUT_SECONDS)
        else:
            uri = f"file:{target}?mode={mode}"
            connection = sqlite3.connect(uri, uri=True, timeout=INDEX_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _promote_database(self, temporary: Path, target: Path) -> None:
        os.chmod(temporary, 0o640)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o640)
        parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
