from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import argparse
import json
import re
import sys

from .catalog import ModelCatalog
from .client import OpenWebUIClient
from .config import load_config
from .dashboard import serve
from .orchestrator import SwarmOrchestrator
from .prompts import authority_block, worker_prompt
from .quality import benchmark_by_id, deterministic_checks, load_benchmarks


SECRET_NAMES = {".env", "auth.json", "credentials", "credentials.json", "secrets.json"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|nvapi)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def _read_text(path: str) -> str:
    file_path = Path(path).expanduser().resolve()
    name = file_path.name.lower()
    if name in SECRET_NAMES or name.startswith(".env.") or file_path.suffix.lower() in SECRET_SUFFIXES:
        raise RuntimeError(f"Refusing likely secret file: {file_path}")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Context file is not UTF-8 text: {file_path}") from exc
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise RuntimeError(f"Refusing file containing a likely secret: {file_path}")
    return content


def _load_context(paths: Iterable[str]) -> list[tuple[str, str]]:
    return [(str(Path(path).expanduser()), _read_text(path)) for path in paths]


def _assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected ROLE=MODEL, received: {value}")
        role, model = value.split("=", 1)
        if not role.strip() or not model.strip():
            raise ValueError(f"Expected ROLE=MODEL, received: {value}")
        result[role.strip()] = model.strip()
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="owui-swarm",
        description="Delegate compact task packets to selectable Open WebUI models and return one judged result.",
    )
    parser.add_argument("--config", default="~/.config/owui-swarm/config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check Open WebUI health and synchronize the model catalog.")
    models = sub.add_parser("models", help="Synchronize and list Open WebUI models with local classifications.")
    models.add_argument("--json", action="store_true")

    probe = sub.add_parser("probe", help="Test chat compatibility and latency for selected models.")
    probe.add_argument("model", nargs="*")
    probe.add_argument("--enabled", action="store_true", help="Probe every enabled chat model.")

    dashboard = sub.add_parser("serve", help="Run the local swarm monitoring and dispatch dashboard.")
    dashboard.add_argument("--host")
    dashboard.add_argument("--port", type=int)

    benchmark = sub.add_parser("benchmark", help="Run or list the compact local quality benchmarks.")
    benchmark.add_argument("benchmark_id", nargs="?")
    benchmark.add_argument("--list", action="store_true")
    benchmark.add_argument("--model")
    benchmark.add_argument(
        "--role", choices=["planner", "implementer", "critic", "verifier", "__judge__"]
    )
    benchmark.add_argument("--max-tokens", type=int, default=600)

    run = sub.add_parser("run", help="Run a parallel worker swarm and subordinate judge.")
    prompt_group = run.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    prompt_group.add_argument("--stdin", action="store_true")
    run.add_argument("--mode", choices=["auto", "code", "spec", "research", "general"], default="auto")
    run.add_argument("--acceptance", default="")
    run.add_argument("--context", action="append", default=[], metavar="FILE")
    run.add_argument("--worker", action="append", default=[], help="Use only the named configured role.")
    run.add_argument(
        "--role-model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="Override a role's model for this run. Repeat as needed.",
    )
    run.add_argument("--judge-model", help="Override the judge model for this run.")
    run.add_argument(
        "--auto-models",
        type=int,
        default=0,
        help="Assign the top N locally catalogued models across worker roles for this mode.",
    )
    run.add_argument("--json", action="store_true")

    wiki = sub.add_parser("wiki", help="Manage the separate versioned local wiki repository.")
    wiki.add_argument("--root", help="Wiki root; defaults to OWUI_SWARM_WIKI_ROOT or /srv/swarm-wiki.")
    wiki_sub = wiki.add_subparsers(dest="wiki_command", required=True)
    wiki_init = wiki_sub.add_parser("init", help="Initialize an empty wiki root without Git.")
    wiki_init.add_argument("--with-samples", action="store_true")
    wiki_sub.add_parser("validate", help="Validate schemas, paths, references, and repository state.")
    wiki_status = wiki_sub.add_parser("status", help="Show compact wiki and Git status.")
    wiki_status.add_argument("--backup-root")
    wiki_index = wiki_sub.add_parser("index", help="Build or update the derived SQLite FTS5 wiki index.")
    wiki_index.add_argument("--full", action="store_true", help="Force a clean rebuild instead of an incremental update.")
    wiki_index.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    wiki_get = wiki_sub.add_parser("get", help="Retrieve one exact canonical page.")
    wiki_get.add_argument("page_id")
    wiki_sub.add_parser("list", help="List canonical page metadata in deterministic order.")
    wiki_search = wiki_sub.add_parser("search", help="Query the current derived wiki search index.")
    wiki_search.add_argument("query")
    wiki_search.add_argument("--limit", type=int, default=20)
    wiki_search.add_argument("--verification")
    wiki_search.add_argument("--min-confidence", type=int)
    wiki_search.add_argument("--jira-key")
    wiki_search.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    wiki_related = wiki_sub.add_parser("related", help="List related canonical pages for one page ID.")
    wiki_related.add_argument("page_id")
    wiki_related.add_argument("--limit", type=int, default=10)
    wiki_related.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    wiki_stale = wiki_sub.add_parser("stale", help="List pages whose source timestamps are older than N days.")
    wiki_stale.add_argument("--days", type=int, required=True)
    wiki_stale.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    wiki_backup = wiki_sub.add_parser("backup", help="Create a restricted Git and working-tree backup.")
    wiki_backup.add_argument("--backup-root")
    wiki_restore = wiki_sub.add_parser(
        "restore-verify", help="Verify a backup in a new temporary directory."
    )
    wiki_restore.add_argument("backup")
    return parser


def _run_wiki(args: argparse.Namespace) -> int:
    from .wiki import WikiRepository
    from .wiki_search import WikiIndex

    repository = WikiRepository(args.root)
    index = WikiIndex(repository)
    if args.wiki_command == "init":
        print(json.dumps(
            repository.initialize(with_samples=args.with_samples),
            indent=2, ensure_ascii=False,
        ))
        return 0
    if args.wiki_command == "validate":
        issues = repository.validate()
        for issue in issues:
            print("\t".join((
                issue.code, issue.file, issue.item_id, issue.field, issue.message
            )))
        return 1 if issues else 0
    if args.wiki_command == "status":
        print(json.dumps(
            repository.status(args.backup_root), indent=2, ensure_ascii=False
        ))
        return 0
    if args.wiki_command == "index":
        print(json.dumps(index.build(full=args.full), indent=2, ensure_ascii=False))
        return 0
    if args.wiki_command == "get":
        page = repository.get_page(args.page_id)
        print(json.dumps(
            {"metadata": page.metadata(), "content": page.body},
            indent=2, ensure_ascii=False,
        ))
        return 0
    if args.wiki_command == "list":
        print(json.dumps(repository.list_pages(), indent=2, ensure_ascii=False))
        return 0
    if args.wiki_command == "search":
        print(json.dumps(index.search(
            args.query,
            limit=args.limit,
            verification=args.verification,
            min_confidence=args.min_confidence,
            jira_key=args.jira_key,
        ), indent=2, ensure_ascii=False))
        return 0
    if args.wiki_command == "related":
        print(json.dumps(index.related(args.page_id, limit=args.limit), indent=2, ensure_ascii=False))
        return 0
    if args.wiki_command == "stale":
        print(json.dumps(index.stale(days=args.days), indent=2, ensure_ascii=False))
        return 0
    if args.wiki_command == "backup":
        print(json.dumps(
            {"backup": str(repository.backup(args.backup_root))}, indent=2
        ))
        return 0
    if args.wiki_command == "restore-verify":
        print(json.dumps(
            WikiRepository.restore_verify(args.backup), indent=2, ensure_ascii=False
        ))
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "wiki":
            return _run_wiki(args)
        config = load_config(args.config)
        client = OpenWebUIClient(
            config.openwebui.base_url,
            config.openwebui.endpoint,
            config.openwebui.api_key_env,
            config.openwebui.timeout_seconds,
            config.openwebui.health_endpoint,
            config.openwebui.models_endpoint,
        )
        catalog = ModelCatalog(config.swarm.catalog_path)
        catalog.import_run_history(config.swarm.run_directory)

        if args.command == "benchmark":
            if args.list:
                for item in load_benchmarks():
                    print(f"{item['id']}\t{item['role']}\t{item['mode']}\t{item['task']}")
                return 0
            if not args.benchmark_id or not args.model or not args.role:
                raise RuntimeError("Benchmark execution requires BENCHMARK_ID, --model, and --role.")
            benchmark = benchmark_by_id(args.benchmark_id)
            record = catalog.get(args.model)
            if not record or not (record.enabled and record.available and record.kind == "chat" and record.probe_status == "healthy"):
                raise RuntimeError("Benchmark model must be an enabled, available, healthy chat model.")
            agents = {worker.name: worker for worker in config.workers}
            agent = config.judge if args.role == "__judge__" else agents[args.role]
            system = f"{agent.system.strip()}\n\n{authority_block(config.authority)}"
            user = worker_prompt(
                benchmark["task"], benchmark["mode"], benchmark["acceptance"], "", config.authority
            )
            timeout = config.swarm.judge_timeout_seconds if args.role == "__judge__" else config.swarm.worker_timeout_seconds
            result = client.chat(
                args.model, system, user, max(64, min(args.max_tokens, 800)), 0.0,
                timeout_seconds=timeout,
            )
            stamp = datetime.now(timezone.utc).strftime("bench-%Y%m%dT%H%M%S%fZ")
            safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", args.model)
            output = Path(config.swarm.run_directory).expanduser().resolve().parent / "benchmarks" / stamp / safe_model
            output.mkdir(parents=True, mode=0o700)
            response_path = output / f"{benchmark['id']}.md"
            response_path.write_text(result.content + "\n", encoding="utf-8")
            response_path.chmod(0o600)
            checks = deterministic_checks(benchmark, result.content)
            result_path = output / f"{benchmark['id']}.json"
            result_path.write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
            result_path.chmod(0o600)
            catalog.record_benchmark_result(
                benchmark["id"], stamp, args.model, args.role, benchmark["mode"],
                str(response_path), checks, {}, "deterministic",
                "Awaiting Codex review; deterministic checks only.",
            )
            print(json.dumps({"run_id": stamp, "response": str(response_path), "checks": checks}, ensure_ascii=False))
            return 0

        if args.command == "doctor":
            health = client.health()
            records = catalog.sync(client.list_model_entries())
            print(json.dumps({"health": health, "model_count": len(records), "catalog": str(catalog.path)}, indent=2))
            return 0

        if args.command == "models":
            records = catalog.sync(client.list_model_entries())
            payload = [catalog.as_dict(record, config.reliability) for record in records]
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                for record in records:
                    caps = ",".join(record.capabilities)
                    latency = f" {record.probe_ms}ms" if record.probe_ms is not None else ""
                    print(f"{record.model_id}\t{record.kind}\t{caps}\t{record.probe_status}{latency}\t{'enabled' if record.enabled else 'disabled'}")
            return 0

        if args.command == "probe":
            catalog.sync(client.list_model_entries())
            ids = list(args.model)
            if args.enabled:
                ids.extend(record.model_id for record in catalog.list() if record.enabled and record.kind == "chat")
            ids = sorted(set(ids))
            if not ids:
                raise RuntimeError("Provide model IDs or use --enabled.")
            def probe_one(model_id: str) -> tuple[str, str, int, str]:
                started = monotonic()
                try:
                    result = client.chat(
                        model_id, "Return only the requested token.",
                        "Return exactly: HEALTHY", 20, 0.0,
                        timeout_seconds=config.probe.timeout_seconds,
                    )
                    elapsed = int((monotonic() - started) * 1000)
                    status = "healthy" if result.content.strip() == "HEALTHY" else "failed"
                    detail = "" if status == "healthy" else f"Unexpected response: {result.content[:200]}"
                except Exception as exc:
                    elapsed = int((monotonic() - started) * 1000)
                    status, detail = "failed", str(exc)
                catalog.record_probe(model_id, status, elapsed, detail)
                return model_id, status, elapsed, detail

            with ThreadPoolExecutor(
                max_workers=min(config.probe.max_parallel, len(ids))
            ) as executor:
                futures = [executor.submit(probe_one, model_id) for model_id in ids]
                for future in as_completed(futures):
                    model_id, status, elapsed, detail = future.result()
                    suffix = f": {detail}" if detail else ""
                    print(f"{model_id}: {status} ({elapsed} ms){suffix}")
            return 0

        if args.command == "serve":
            serve(config, args.host, args.port)
            return 0

        if args.command == "run":
            if args.prompt is not None:
                objective = args.prompt
            elif args.prompt_file is not None:
                objective = _read_text(args.prompt_file)
            else:
                objective = sys.stdin.read()
            if not objective.strip():
                raise RuntimeError("The task objective is empty.")

            role_overrides = _assignments(args.role_model)
            configured_roles = {worker.name for worker in config.workers}
            unknown = set(role_overrides) - configured_roles
            if unknown:
                raise ValueError("Unknown worker role(s): " + ", ".join(sorted(unknown)))
            if role_overrides and not config.reliability.allow_explicit_override:
                raise RuntimeError("Explicit model overrides are disabled by reliability policy.")
            selection_reasons = {
                role: catalog.explicit_override_reason(catalog.get(model), config.reliability)
                for role, model in role_overrides.items()
            }
            requested_workers = args.worker or None
            judge_model = args.judge_model
            if judge_model:
                if not config.reliability.allow_explicit_override:
                    raise RuntimeError("Explicit model overrides are disabled by reliability policy.")
                selection_reasons["__judge__"] = catalog.explicit_override_reason(
                    catalog.get(judge_model), config.reliability
                )
            if args.auto_models:
                if args.auto_models < 1 or args.auto_models > config.swarm.max_workers:
                    raise ValueError(
                        f"--auto-models must be between 1 and {config.swarm.max_workers}."
                    )
                roles = [w.name for w in config.workers if args.mode in w.modes or "auto" in w.modes]
                requested_workers = roles[: min(args.auto_models, len(roles))]
                used_models = set(role_overrides.values())
                used_families = {
                    record.family for model in used_models
                    if (record := catalog.get(model)) is not None
                }
                for role in requested_workers:
                    if role not in role_overrides:
                        candidates = catalog.recommend(
                            args.mode, 1, config.reliability, role,
                            excluded_models=used_models, used_families=used_families,
                        )
                        if not candidates:
                            raise RuntimeError(
                                "Not enough enabled, compatible, healthy models outside reliability cooldown for automatic routing."
                            )
                        record = candidates[0]
                        role_overrides[role] = record.model_id
                        used_models.add(record.model_id)
                        used_families.add(record.family)
                        selection_reasons[role] = catalog.recommendation_reason(
                            record, args.mode, config.reliability, role
                        )
                if not judge_model:
                    candidates = catalog.recommend(
                        args.mode, 1, config.reliability, "__judge__",
                        excluded_models=used_models, used_families=used_families,
                    )
                    if not candidates:
                        raise RuntimeError("Automatic routing requires a separate healthy judge model.")
                    judge_record = candidates[0]
                    judge_model = judge_record.model_id
                    selection_reasons["__judge__"] = catalog.recommendation_reason(
                        judge_record, args.mode, config.reliability, "__judge__"
                    )

            orchestrator = SwarmOrchestrator(config)
            bounded, run_dir, parsed = orchestrator.run(
                objective=objective,
                mode=args.mode,
                acceptance=args.acceptance,
                context_parts=_load_context(args.context),
                requested_workers=requested_workers,
                role_model_overrides=role_overrides,
                judge_model_override=judge_model,
                selection_reasons=selection_reasons,
            )
            if args.json:
                print(json.dumps({"run_directory": str(run_dir), "final_markdown": str(run_dir / "final.md"), "result": parsed}, ensure_ascii=False))
            else:
                print(bounded)
                print(f"\nSWARM_RUN_DIR={run_dir}")
                print(f"SWARM_FINAL={run_dir / 'final.md'}")
            return 0

        return 2
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
