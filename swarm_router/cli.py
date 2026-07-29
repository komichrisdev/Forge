from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import argparse
import json
import os
import re
import sys
from urllib import error, request

from .agents import AgentManifest, HandoffEnvelope, default_registry, load_json_object
from .catalog import ModelCatalog
from .client import OpenWebUIClient
from .config import load_config
from .dashboard import serve
from .discord_notifications import NotificationStore, deliver, load_config as load_discord_config, notification_from_store
from .image_generation import ComfyUIClient, PRESET_ID, gallery as image_gallery, preset_summary, validate_image_payload
from .journal import TaskJournal
from .orchestrator import SwarmOrchestrator
from .personal import serve_personal
from .prompts import authority_block, worker_prompt
from .providers import OpenAICompatibleProvider, provider_items
from .quality import benchmark_by_id, deterministic_checks, load_benchmarks
from .scheduler import Schedule, ScheduleError, Scheduler, ScheduleStore, install_signal_handlers, validate_schedule


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
    status = sub.add_parser("status", help="Show read-only Forge registry status.")
    status.add_argument("--json", action="store_true")

    agent = sub.add_parser("agent", help="Inspect logical Forge agents.")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = agent_sub.add_parser("list", help="List registered logical agents.")
    agent_list.add_argument("--json", action="store_true")
    agent_show = agent_sub.add_parser("show", help="Show one registered logical agent.")
    agent_show.add_argument("agent_id")
    agent_show.add_argument("--json", action="store_true")
    agent_validate = agent_sub.add_parser("validate", help="Validate the built-in registry or one manifest JSON file.")
    agent_validate.add_argument("manifest", nargs="?")
    agent_validate.add_argument("--json", action="store_true")

    handoff = sub.add_parser("handoff", help="Inspect worker handoff envelopes.")
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_validate = handoff_sub.add_parser("validate", help="Validate a handoff envelope JSON file or stdin.")
    handoff_validate.add_argument("envelope", help="Path to JSON envelope, or '-' for stdin.")
    handoff_validate.add_argument("--json", action="store_true")

    journal = sub.add_parser("journal", help="Inspect the persistent Forge task journal.")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)
    journal_list = journal_sub.add_parser("list", help="List journaled tasks.")
    journal_list.add_argument("--json", action="store_true")
    journal_show = journal_sub.add_parser("show", help="Show reconstructed state for one task.")
    journal_show.add_argument("task_id")
    journal_show.add_argument("--json", action="store_true")
    journal_events = journal_sub.add_parser("events", help="Show append-only events for one task.")
    journal_events.add_argument("task_id")
    journal_events.add_argument("--json", action="store_true")
    journal_checkpoints = journal_sub.add_parser("checkpoints", help="Show checkpoint records for one task.")
    journal_checkpoints.add_argument("task_id")
    journal_checkpoints.add_argument("--json", action="store_true")
    journal_orphans = journal_sub.add_parser("orphans", help="Show read-only suspected orphan tasks.")
    journal_orphans.add_argument("--json", action="store_true")
    journal_recovery = journal_sub.add_parser("recovery-status", help="Show replay-safety status for one task.")
    journal_recovery.add_argument("task_id")
    journal_recovery.add_argument("--json", action="store_true")

    schedule = sub.add_parser("schedule", help="Manage persistent Forge automation schedules.")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_list = schedule_sub.add_parser("list", help="List schedules.")
    schedule_list.add_argument("--json", action="store_true")
    schedule_show = schedule_sub.add_parser("show", help="Show one schedule.")
    schedule_show.add_argument("schedule_id")
    schedule_show.add_argument("--json", action="store_true")
    schedule_create = schedule_sub.add_parser("create", help="Create a schedule from JSON file or stdin.")
    schedule_create.add_argument("schedule_config", help="Path to JSON schedule config, or '-' for stdin.")
    schedule_create.add_argument("--json", action="store_true")
    schedule_validate = schedule_sub.add_parser("validate", help="Validate a schedule JSON file or stdin.")
    schedule_validate.add_argument("schedule_config", help="Path to JSON schedule config, or '-' for stdin.")
    schedule_validate.add_argument("--json", action="store_true")
    schedule_enable = schedule_sub.add_parser("enable", help="Enable a schedule.")
    schedule_enable.add_argument("schedule_id")
    schedule_enable.add_argument("--json", action="store_true")
    schedule_disable = schedule_sub.add_parser("disable", help="Disable a schedule without deleting history.")
    schedule_disable.add_argument("schedule_id")
    schedule_disable.add_argument("--json", action="store_true")
    schedule_run_now = schedule_sub.add_parser("run-now", help="Create one immediate personal task for a schedule.")
    schedule_run_now.add_argument("schedule_id")
    schedule_run_now.add_argument("--json", action="store_true")
    schedule_occurrences = schedule_sub.add_parser("occurrences", help="List schedule occurrences.")
    schedule_occurrences.add_argument("schedule_id")
    schedule_occurrences.add_argument("--json", action="store_true")

    scheduler = sub.add_parser("scheduler", help="Run or inspect the local Forge scheduler.")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_status = scheduler_sub.add_parser("status", help="Show scheduler status.")
    scheduler_status.add_argument("--json", action="store_true")
    scheduler_tick = scheduler_sub.add_parser("tick", help="Process due schedules once.")
    scheduler_tick.add_argument("--json", action="store_true")
    scheduler_run = scheduler_sub.add_parser("run", help="Run the foreground scheduler loop.")
    scheduler_run.add_argument("--json", action="store_true")

    discord = sub.add_parser("discord", help="Inspect or test Forge Discord notifications.")
    discord_sub = discord.add_subparsers(dest="discord_command", required=True)
    discord_status = discord_sub.add_parser("status", help="Show redacted Discord configuration and delivery status.")
    discord_status.add_argument("--json", action="store_true")
    discord_test = discord_sub.add_parser("test", help="Send one clearly labelled Discord test notification.")
    discord_test.add_argument("--deduplication-key", help="Optional stable key for duplicate-suppression validation.")
    discord_test.add_argument("--json", action="store_true")

    notification = sub.add_parser("notification", help="Inspect persisted Forge notification deliveries.")
    notification_sub = notification.add_subparsers(dest="notification_command", required=True)
    notification_list = notification_sub.add_parser("list", help="List notification deliveries.")
    notification_list.add_argument("--json", action="store_true")
    notification_show = notification_sub.add_parser("show", help="Show one notification delivery.")
    notification_show.add_argument("notification_id")
    notification_show.add_argument("--json", action="store_true")

    image = sub.add_parser("image", help="Use approved local Forge image generation.")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_status = image_sub.add_parser("status", help="Show ComfyUI and image artifact status.")
    image_status.add_argument("--json", action="store_true")
    image_presets = image_sub.add_parser("presets", help="List approved image presets.")
    image_presets.add_argument("--json", action="store_true")
    image_generate = image_sub.add_parser("generate", help="Submit one approved image generation task.")
    image_generate.add_argument("--prompt", required=True)
    image_generate.add_argument("--negative-prompt", default="")
    image_generate.add_argument("--seed")
    image_generate.add_argument("--notify-discord", action="store_true")
    image_generate.add_argument("--confirm", default="")
    image_generate.add_argument("--json", action="store_true")
    image_jobs = image_sub.add_parser("jobs", help="List recent image generation jobs.")
    image_jobs.add_argument("--json", action="store_true")
    image_show = image_sub.add_parser("show", help="Show one image generation Forge task.")
    image_show.add_argument("forge_task_id")
    image_show.add_argument("--json", action="store_true")

    models = sub.add_parser("models", help="Synchronize and list Open WebUI models with local classifications.")
    models.add_argument("--json", action="store_true")

    probe = sub.add_parser("probe", help="Test chat compatibility and latency for selected models.")
    probe.add_argument("model", nargs="*")
    probe.add_argument("--enabled", action="store_true", help="Probe every enabled chat model.")

    provider = sub.add_parser("provider", help="Inspect and reconcile provider model inventories.")
    provider.add_argument("--provider-id", default="nvidia")
    provider_sub = provider.add_subparsers(dest="provider_command", required=True)
    provider_status = provider_sub.add_parser("status", help="Show provider inventory and cooldown state.")
    provider_status.add_argument("--json", action="store_true")
    provider_refresh = provider_sub.add_parser("refresh", help="Fetch provider inventory and reconcile it.")
    provider_refresh.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    provider_refresh.add_argument("--json", action="store_true")
    provider_diff = provider_sub.add_parser("diff", help="Show quarantined or missing provider models.")
    provider_diff.add_argument("--json", action="store_true")
    provider_probe = provider_sub.add_parser("probe", help="Probe provider models that need validation.")
    provider_probe.add_argument("--new-and-recovered", action="store_true")
    provider_probe.add_argument("--limit", type=int, default=8)
    provider_probe.add_argument("model", nargs="*")
    provider_cooldown = provider_sub.add_parser(
        "cooldown", aliases=["cooldowns"], help="Show or set provider cooldown state."
    )
    provider_cooldown.add_argument("--minutes", type=int)
    provider_cooldown.add_argument("--clear", action="store_true")
    provider_cooldown.add_argument("--clear-model")
    provider_cooldown.add_argument("--json", action="store_true")

    dashboard = sub.add_parser("serve", help="Run the local swarm monitoring and dispatch dashboard.")
    dashboard.add_argument("--host")
    dashboard.add_argument("--port", type=int)

    sub.add_parser("personal-serve", help="Run the personal-task OpenAI-compatible backend.")

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
    wiki_page = wiki_sub.add_parser("page", help="Retrieve one exact canonical page with sources and relationships.")
    page_selector = wiki_page.add_mutually_exclusive_group(required=True)
    page_selector.add_argument("--page-id")
    page_selector.add_argument("--slug")
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
    if args.wiki_command == "page":
        print(json.dumps(
            repository.page_view(page_id=args.page_id, slug=args.slug),
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


def _read_json_file(path: str) -> dict[str, object]:
    return load_json_object(sys.stdin.read() if path == "-" else _read_text(path))


def _print_validation(name: str, issues: list[str], as_json: bool) -> int:
    if as_json:
        print(json.dumps({"valid": not issues, "target": name, "issues": issues}, indent=2, ensure_ascii=False))
    elif issues:
        for issue in issues:
            print(f"{name}\t{issue}")
    else:
        print(f"{name}\tvalid")
    return 1 if issues else 0


def _run_agent(args: argparse.Namespace) -> int:
    registry = default_registry()
    if args.command == "status":
        payload = {"forge_version": "0.12-dev", "architecture_revision": "R12", **registry.status()}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"Forge 0.12-dev\tArchitecture R12\t"
                f"agents={payload['agent_count']}\tenabled={payload['enabled_count']}"
            )
        return 0
    if args.command == "agent":
        if args.agent_command == "list":
            agents = [agent.to_dict() for agent in registry.list()]
            if args.json:
                print(json.dumps(agents, indent=2, ensure_ascii=False))
            else:
                for agent in registry.list():
                    print(f"{agent.agent_id}\t{agent.display_name}\t{'enabled' if agent.enabled else 'disabled'}")
            return 0
        if args.agent_command == "show":
            agent = registry.get(args.agent_id)
            if agent is None:
                raise RuntimeError(f"Unknown agent: {args.agent_id}")
            if args.json:
                print(json.dumps(agent.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(f"{agent.agent_id}\t{agent.display_name}\t{agent.version}\t{agent.owner}")
                print(agent.description)
            return 0
        if args.agent_command == "validate":
            if args.manifest:
                manifest = AgentManifest.from_dict(_read_json_file(args.manifest))
                return _print_validation(args.manifest, manifest.validate(), args.json)
            return _print_validation("agent registry", registry.validate(), args.json)
    if args.command == "handoff" and args.handoff_command == "validate":
        envelope = HandoffEnvelope.from_dict(_read_json_file(args.envelope))
        return _print_validation(args.envelope, envelope.validate(registry), args.json)
    return 2


def _run_journal(args: argparse.Namespace) -> int:
    config = load_config(args.config, require_api_key=False)
    journal = TaskJournal(config.swarm.catalog_path)
    if args.journal_command == "list":
        rows = journal.list_tasks()
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for item in rows:
                print(f"{item['task_id']}\t{item['status']}\t{item['event_count']}\t{item['updated_at']}")
        return 0
    if args.journal_command == "show":
        item = journal.reconstruct(args.task_id)
        if args.json:
            print(json.dumps(item, indent=2, ensure_ascii=False))
        else:
            print(f"{item['task_id']}\t{item['status']}\t{item['event_count']}")
            print(f"agents\t{','.join(item['agents']) or '-'}")
        return 0
    if args.journal_command == "events":
        rows = [event.to_dict() for event in journal.events(args.task_id)]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for event in rows:
                print(f"{event['sequence']}\t{event['timestamp']}\t{event['event_type']}\t{event['agent_id']}\t{event['message']}")
        return 0
    if args.journal_command == "checkpoints":
        rows = [checkpoint.to_dict() for checkpoint in journal.checkpoints(args.task_id)]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for item in rows:
                print(f"{item['timestamp']}\t{item['task_id']}\t{item['stage']}\t{item['agent_id']}\t{item['checkpoint_reference']}")
        return 0
    if args.journal_command == "orphans":
        rows = journal.orphan_candidates()
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for item in rows:
                print(f"{item['task_id']}\t{item['orphan_status']}\t{item.get('lease_expires_at', '-')}")
        return 0
    if args.journal_command == "recovery-status":
        item = journal.recovery_status(args.task_id)
        if args.json:
            print(json.dumps(item, indent=2, ensure_ascii=False))
        else:
            print(f"{item['task_id']}\t{item['status']}\t{item['replay_safety']}\trecovery_allowed={item['recovery_allowed']}")
        return 0
    return 2


def _schedule_json(path: str, default_timezone: str) -> dict[str, object]:
    data = _read_json_file(path)
    data.setdefault("timezone", default_timezone)
    return data


def _print_schedule_rows(rows: list[dict[str, object]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for item in rows:
        last = item.get("last_occurrence") or {}
        task_id = last.get("task_id", "-") if isinstance(last, dict) else "-"
        print(
            f"{item['schedule_id']}\t{item['state']}\t"
            f"enabled={item['enabled']}\tnext={item['next_run_at'] or '-'}\t"
            f"tz={item['timezone']}\tlast_task={task_id or '-'}\t{item['name']}"
        )


def _run_schedule(args: argparse.Namespace) -> int:
    needs_execution = args.schedule_command == "run-now"
    config = load_config(args.config, require_api_key=needs_execution)
    store = ScheduleStore(config.swarm.catalog_path)
    if args.schedule_command == "list":
        _print_schedule_rows(store.status()["schedules"], args.json)
        return 0
    if args.schedule_command == "show":
        item = store.get(args.schedule_id).to_dict()
        item["occurrences"] = store.occurrences(args.schedule_id)[-5:]
        if args.json:
            print(json.dumps(item, indent=2, ensure_ascii=False))
        else:
            print(f"{item['schedule_id']}\t{item['name']}\tenabled={item['enabled']}\tnext={item['next_run_at'] or '-'}\ttz={item['timezone']}")
            print(item["description"])
        return 0
    if args.schedule_command == "create":
        item = store.create(_schedule_json(args.schedule_config, config.scheduler.timezone))
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"{item.schedule_id}\tcreated\tnext={item.next_run_at or '-'}\ttz={item.timezone}")
        return 0
    if args.schedule_command == "validate":
        raw = _schedule_json(args.schedule_config, config.scheduler.timezone)
        schedule = Schedule.from_dict(raw)
        issues = validate_schedule(schedule, allow_empty_id=True)
        return _print_validation(args.schedule_config, issues, args.json)
    if args.schedule_command == "enable":
        item = store.set_enabled(args.schedule_id, True)
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"{item.schedule_id}\tenabled")
        return 0
    if args.schedule_command == "disable":
        item = store.set_enabled(args.schedule_id, False)
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"{item.schedule_id}\tdisabled")
        return 0
    if args.schedule_command == "run-now":
        occurrence = Scheduler(config, store=store).run_once(args.schedule_id)
        if args.json:
            print(json.dumps(occurrence, indent=2, ensure_ascii=False))
        else:
            print(f"{occurrence['occurrence_id']}\t{occurrence['status']}\ttask={occurrence['task_id'] or '-'}")
        return 0
    if args.schedule_command == "occurrences":
        rows = store.occurrences(args.schedule_id)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for item in rows:
                print(f"{item['scheduled_for']}\t{item['status']}\ttask={item['task_id'] or '-'}\t{item['occurrence_id']}")
        return 0
    return 2


def _run_scheduler(args: argparse.Namespace) -> int:
    config = load_config(args.config, require_api_key=False)
    scheduler = Scheduler(config)
    if args.scheduler_command == "status":
        payload = scheduler.store.status(scheduler.task_status)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            _print_schedule_rows(payload["schedules"], False)
        return 0
    if args.scheduler_command == "tick":
        result = scheduler.tick()
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"locked={result['locked']}\tprocessed={len(result['processed'])}")
        return 0 if result["locked"] else 1
    if args.scheduler_command == "run":
        if args.json:
            print(json.dumps({"status": "running", "poll_interval_seconds": config.scheduler.poll_interval_seconds}))
        stop = Event()
        install_signal_handlers(stop)
        scheduler.run_forever(stop)
        return 0
    return 2


def _print_notification_rows(rows: list[dict[str, object]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for item in rows:
        print(
            f"{item['notification_id']}\t{item['status']}\t{item['event_type']}\t"
            f"{item['severity']}\ttask={item['forge_task_id'] or item['task_id'] or '-'}"
        )


def _run_discord(args: argparse.Namespace) -> int:
    config = load_config(args.config, require_api_key=False)
    store = NotificationStore(config.swarm.catalog_path)
    discord = load_discord_config()
    if args.discord_command == "status":
        payload = {"configuration": discord.public(), **store.status()}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"configured={discord.configured}\tvalid={discord.valid}\thost={discord.host or '-'}\tmode={discord.mode or '-'}")
            for issue in discord.issues:
                print(f"issue\t{issue}")
        return 0 if discord.valid else 1
    if args.discord_command == "test":
        key = args.deduplication_key or f"discord-test:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        item = notification_from_store(
            store,
            event_type="discord.test",
            severity="info",
            title="Forge Discord notification test",
            message="This is a single explicit Forge Discord test message.",
            agent_id="manager",
            deduplication_key=key,
            metadata={"manual": True},
        )
        row = deliver(store, item)
        if args.json:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        else:
            print(f"{row['notification_id']}\t{row['status']}\t{row['http_classification']}\texternal={row['external_message_id'] or '-'}")
        return 0 if row["status"] == "confirmed" or row.get("duplicate_suppressed") else 1
    return 2


def _run_notification(args: argparse.Namespace) -> int:
    config = load_config(args.config, require_api_key=False)
    store = NotificationStore(config.swarm.catalog_path)
    if args.notification_command == "list":
        _print_notification_rows(store.list(), args.json)
        return 0
    if args.notification_command == "show":
        row = store.get(args.notification_id)
        if args.json:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        else:
            _print_notification_rows([row], False)
            if row["error_summary"]:
                print(f"error\t{row['error_summary']}")
        return 0
    return 2


def _personal_post(config: Any, body: dict[str, object]) -> dict[str, object]:
    token = os.environ.get(config.personal.auth_token_env, "")
    if not token:
        raise RuntimeError(f"{config.personal.auth_token_env} is required")
    req = request.Request(
        f"http://{config.personal.loopback_host}:{config.personal.port}/api/personal-tasks",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8")[:500]) from exc
    return data if isinstance(data, dict) else {}


def _run_image(args: argparse.Namespace) -> int:
    config = load_config(args.config, require_api_key=False)
    if args.image_command == "presets":
        payload = [preset_summary()]
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            item = payload[0]
            print(f"{item['preset_id']}\t{item['name']}\t{item['width']}x{item['height']}\t{item['model']}")
        return 0
    if args.image_command == "status":
        status = ComfyUIClient(
            config.image_generation.comfyui_base_url,
            connect_timeout=config.image_generation.connect_timeout_seconds,
            request_timeout=config.image_generation.request_timeout_seconds,
        ).status()
        payload = {
            "connection": status.__dict__,
            "preset": preset_summary(),
            "artifact_directory": config.image_generation.artifact_directory,
            "gallery_count": len(image_gallery(config, 100)),
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"comfyui\t{status.state}\tqueue={status.queue_depth}\t{status.detail}")
            print(f"preset\t{PRESET_ID}\tready")
        return 0 if status.state != "offline" else 1
    if args.image_command == "generate":
        if args.confirm != "generate image":
            raise RuntimeError("Confirmation must be exactly: generate image")
        payload = validate_image_payload({
            "preset_id": PRESET_ID,
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt,
            "seed": args.seed,
            "notification_requested": args.notify_discord,
        })
        task = _personal_post(config, {
            "model": config.personal.model_id,
            "messages": [{"role": "user", "content": "Forge CLI image generation request."}],
            "task_type": "image_generate",
            "agent_id": "image_generator",
            "task_payload": payload.__dict__,
            "metadata": {"cli_action": "image_generate", "manual": True, "task_type": "image_generate"},
        })
        if args.json:
            print(json.dumps(task, indent=2, ensure_ascii=False))
        else:
            print(f"{task.get('forge_task_id')}\t{task.get('task_id')}\t{task.get('status')}")
        return 0
    journal = TaskJournal(config.swarm.catalog_path)
    personal_root = Path(config.personal.task_directory).expanduser().resolve()
    personal: dict[str, dict[str, object]] = {}
    if personal_root.exists():
        for path in personal_root.iterdir():
            data = json.loads((path / "task.json").read_text(encoding="utf-8")) if (path / "task.json").exists() else {}
            if isinstance(data, dict):
                personal[path.name] = data
    rows = []
    for task in reversed(journal.list_tasks()):
        events = journal.events(task["task_id"])
        metadata: dict[str, object] = {}
        for event in events:
            metadata.update(event.metadata)
        personal_task_id = str(metadata.get("personal_task_id") or "")
        item = personal.get(personal_task_id, {})
        if item.get("task_type") == "image_generate":
            rows.append({
                **task,
                "personal_task_id": item.get("task_id", ""),
                **{k: item.get(k, "") for k in ("progress", "preset_id", "seed", "comfyui_prompt_id", "artifact_dir", "checksum_sha256")},
            })
    if args.image_command == "jobs":
        if args.json:
            print(json.dumps(rows[:50], indent=2, ensure_ascii=False))
        else:
            for row in rows[:50]:
                print(f"{row['task_id']}\t{row['status']}\tprogress={row.get('progress','')}\tprompt={row.get('comfyui_prompt_id','')}")
        return 0
    if args.image_command == "show":
        row = next((item for item in rows if item["task_id"] == args.forge_task_id), None)
        if row is None:
            raise RuntimeError(f"Unknown image task: {args.forge_task_id}")
        detail = {"task": row, "events": [event.to_dict() for event in journal.events(args.forge_task_id)], "checkpoints": [item.to_dict() for item in journal.checkpoints(args.forge_task_id)]}
        if args.json:
            print(json.dumps(detail, indent=2, ensure_ascii=False))
        else:
            print(f"{row['task_id']}\t{row['status']}\t{row.get('artifact_dir','')}")
            print(f"prompt_id\t{row.get('comfyui_prompt_id','')}")
            print(f"sha256\t{row.get('checksum_sha256','')}")
        return 0
    return 2


def _provider_rows(catalog: ModelCatalog, provider_id: str) -> list[dict[str, object]]:
    return [item for item in catalog.provider_status()["models"] if item["provider_id"] == provider_id]


def _print_provider_status(catalog: ModelCatalog, provider_id: str) -> None:
    status = catalog.provider_status()
    for provider in status["providers"]:
        if provider["provider_id"] == provider_id:
            print(
                f"{provider['provider_id']}\trevision={provider['inventory_revision']}\t"
                f"{provider['last_inventory_status']}\t"
                f"health={provider['health']}\t"
                f"cooldown={provider['cooldown_until'] or '-'}\t"
                f"last_success={provider['last_successful_inventory']}"
            )
            if provider["last_inventory_error"]:
                print(f"error\t{provider['last_inventory_error']}")
    for item in _provider_rows(catalog, provider_id):
        flags = []
        if item["quarantined"]:
            flags.append("quarantine")
        if not item["observed_available"]:
            flags.append(f"missing:{item['consecutive_missing']}")
        if item["cooldown_until"]:
            flags.append(f"cooldown:{item['cooldown_until']}")
        print(
            f"{item['model_id']}\t{item['kind']}\t"
            f"{'available' if item['available'] else 'unavailable'}\t"
            f"{item['health']}\t{','.join(flags) or '-'}"
        )


def _probe_ids(
    args: argparse.Namespace, catalog: ModelCatalog, client: OpenWebUIClient
) -> list[str]:
    if args.model:
        return sorted(set(args.model))
    if args.new_and_recovered:
        rows = [
            item for item in _provider_rows(catalog, args.provider_id)
            if item["observed_available"] and item["quarantined"]
        ]
        return [str(item["model_id"]) for item in rows[: max(1, args.limit)]]
    raise RuntimeError("Provide model IDs or use --new-and-recovered.")


def _probe_model(catalog: ModelCatalog, client: OpenWebUIClient, config, model_id: str) -> tuple[str, str, int, str]:
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "wiki":
            return _run_wiki(args)
        if args.command in {"agent", "handoff", "status"}:
            return _run_agent(args)
        if args.command == "journal":
            return _run_journal(args)
        if args.command == "schedule":
            return _run_schedule(args)
        if args.command == "scheduler":
            return _run_scheduler(args)
        if args.command == "discord":
            return _run_discord(args)
        if args.command == "notification":
            return _run_notification(args)
        if args.command == "image":
            return _run_image(args)
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

        if args.command == "provider":
            if args.provider_command == "status":
                status = catalog.provider_status()
                if args.json:
                    print(json.dumps(status, indent=2, ensure_ascii=False))
                else:
                    _print_provider_status(catalog, args.provider_id)
                return 0
            if args.provider_command == "refresh":
                provider = OpenAICompatibleProvider(args.provider_id, client)
                try:
                    result = catalog.reconcile_inventory(
                        args.provider_id,
                        provider_items(provider.list_models()),
                        mode=args.mode,
                    )
                except Exception as exc:
                    result = catalog.record_inventory_failure(args.provider_id, str(exc))
                if args.json:
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    print(
                        f"{result['provider_id']}\t{result['mode']}\t"
                        f"revision={result['inventory_revision']}\t"
                        f"observed={result.get('observed_count', 0)}"
                    )
                    for key in ("added", "recovered", "missing_once", "unavailable"):
                        values = result.get(key, [])
                        if values:
                            print(f"{key}\t" + "\t".join(map(str, values)))
                    if result.get("error"):
                        print(f"error\t{result['error']}")
                return 0 if result.get("mode") != "failure" else 1
            if args.provider_command == "diff":
                diff = catalog.provider_diff(args.provider_id)
                if args.json:
                    print(json.dumps(diff, indent=2, ensure_ascii=False))
                else:
                    for item in diff:
                        print(
                            f"{item['model_id']}\t"
                            f"{'quarantine' if item['quarantined'] else 'known'}\t"
                            f"missing={item['consecutive_missing']}\t"
                            f"available={item['available']}"
                        )
                return 0
            if args.provider_command == "probe":
                ids = _probe_ids(args, catalog, client)
                for model_id in ids:
                    model_id, status, elapsed, detail = _probe_model(catalog, client, config, model_id)
                    suffix = f": {detail}" if detail else ""
                    print(f"{model_id}: {status} ({elapsed} ms){suffix}")
                return 0
            if args.provider_command in {"cooldown", "cooldowns"}:
                provider_state = catalog.set_provider_cooldown(
                    args.provider_id, minutes=args.minutes, clear=args.clear
                )
                if args.clear_model:
                    catalog.clear_cooldown(args.clear_model)
                rows = [
                    item for item in _provider_rows(catalog, args.provider_id)
                    if item["cooldown_until"]
                ]
                if args.json:
                    print(json.dumps(
                        {"provider": provider_state, "models": rows},
                        indent=2, ensure_ascii=False,
                    ))
                else:
                    print(
                        f"{provider_state['provider_id']}\t"
                        f"cooldown={provider_state['cooldown_until'] or '-'}\t"
                        f"health={provider_state['health']}"
                    )
                    for item in rows:
                        print(f"{item['model_id']}\t{item['cooldown_until']}\t{item['health']}")
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

            with ThreadPoolExecutor(
                max_workers=min(config.probe.max_parallel, len(ids))
            ) as executor:
                futures = [executor.submit(_probe_model, catalog, client, config, model_id) for model_id in ids]
                for future in as_completed(futures):
                    model_id, status, elapsed, detail = future.result()
                    suffix = f": {detail}" if detail else ""
                    print(f"{model_id}: {status} ({elapsed} ms){suffix}")
            return 0

        if args.command == "serve":
            serve(config, args.host, args.port)
            return 0

        if args.command == "personal-serve":
            serve_personal(config)
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
