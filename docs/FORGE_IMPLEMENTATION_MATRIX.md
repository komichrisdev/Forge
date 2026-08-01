# Forge Planning Implementation Matrix

Authoritative source: Planning tab, spreadsheet
`1YIZn_4FE2aqJD5RX4nj-qw5nCZ2-u0UoYB1HCocKY84`
Planning tab ID: `1849918313`
Last live read: 2026-08-01

Status values are evidence states, not estimates: `MAPPED`, `IN_PROGRESS`,
`REVIEW`, `COMPLETE`, or `BLOCKED`.

| # | Planning row | Task | Dependencies | Writer | Branch / commit | Status | Verification | Luna Architecture | Luna Product | Remaining risk |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Shared sticky page/table headers | FG-010 | FG-000 | Qwen Code | pending isolated commit | MAPPED | `test_dashboard`, browser | pending | pending | nested scroll/z-index/mobile |
| 2 | Overview Waifu PNG panel | FG-080 | FG-010 | Qwen Code | pending isolated commit | MAPPED | dashboard/static/auth tests, browser | pending | pending | authorized path, fallback, work-safe control |
| 3 | Durable Active Runs display | FG-020 | FG-010 | SOL | pending | MAPPED | journal/developer/personal/dashboard tests | pending | pending | stale and inferred run sources |
| 4 | Prompt Assistant chat/model controls | FG-090 | FG-060 | SOL | pending | MAPPED | routing/auth/API/browser tests | pending | pending | prompt-only eligibility and no silent generation |
| 5 | Enhance, Rewrite, Expand | FG-100 | FG-090 | Qwen Code | pending isolated commit | MAPPED | prompt diff/original/model tests, browser | pending | pending | preserving original and scoped ignore |
| 6 | Image loading/progress states | FG-030 | FG-010 | Qwen Code | pending isolated commit | MAPPED | image/personal/dashboard tests, browser refresh | pending | pending | cancellation truth and no fake percentages |
| 7 | Previous-image gallery | FG-040 | FG-030 | SOL | pending | MAPPED | pagination/auth/retention/scale/browser tests | pending | pending | current API scans direct children only |
| 8 | Reuse image settings | FG-040 | FG-030 | Qwen Code | pending isolated commit | MAPPED | metadata round-trip/preset compatibility/browser | pending | pending | unavailable preset versions |
| 9 | Delete one image | FG-140 | FG-120 | SOL | pending | MAPPED | auth/CSRF/path/audit/trash/flag tests | pending | pending | destructive fail-closed contract absent |
| 10 | Scoped delete all/restore | FG-170 | FG-140, FG-150 | SOL | pending | MAPPED | scope/count/size/typed-confirm/restore tests | pending | pending | protected/pinned and retention policy |
| 11 | Bounded download all ZIP | FG-150 | FG-120 | SOL | pending | MAPPED | auth/manifest/count/size/disk/memory/timeout tests | pending | pending | streamed archive resource limits |
| 12 | Virtual collections | FG-120 | FG-040 | SOL | pending | MAPPED | collection CRUD/concurrency/provenance tests | pending | pending | migration and immutable masters |
| 13 | Prompt library | FG-110 | FG-100 | Qwen Code | pending isolated commit | MAPPED | version/conflict/order/composition/browser tests | pending | pending | saved text remains non-executable |
| 14 | Structured prompt GUI | FG-110 | FG-100 | Qwen Code | pending isolated commit | MAPPED | deterministic composer/template/raw tests | pending | pending | preset-family compatibility |
| 15 | Structured fields as visual metadata/plain text | FG-110 | FG-100 | Qwen Code | pending isolated commit | MAPPED | exact text/metadata/API preview tests | pending | pending | reproducibility across versions |
| 16 | Sequential prompt queue | FG-130 | FG-110, FG-120 | SOL | pending | MAPPED | one-GPU/restart/pause/cancel/isolation tests | pending | pending | durable recovery and quota semantics |
| 17 | Unified Logs presentation | FG-050 | FG-020 | Qwen Code | pending isolated commit | MAPPED | chronology/filter/pagination/auth/legacy-link tests | pending | pending | preserve notification/audit storage |
| 18 | Agents & Models | FG-060 | FG-020 | Qwen Code | pending isolated commit | MAPPED | registry/catalog/health/fallback/dashboard tests | pending | pending | do not conflate agents and provider models |
| 19 | Night Owl as Dispatch profile | FG-160 | FG-070 | SOL | pending | MAPPED | profile/schedule/handoff/approval tests | pending | pending | stricter worktree/deadline policy |
| 20 | Expandable Forge task rows | FG-070 | FG-050, FG-060 | Qwen Code | pending isolated commit | MAPPED | bounded detail/pagination/redaction/browser tests | pending | pending | full terminal and secret leakage |
| 21 | Separate Night Owl from Jira | FG-070 | FG-050, FG-060 | SOL | pending | MAPPED | compatibility/migration/rollback tests | pending | pending | existing record interpretation |
| 22 | First-class work items | FG-180 | FG-070, FG-160 | SOL | pending | MAPPED | permission/version/audit/migration/browser tests | pending | pending | must not duplicate execution ledger |
| 23 | Task attachments | FG-190 | FG-180 | SOL | pending | MAPPED | MIME/size/hash/quota/malware/retention/auth tests | pending | pending | decompression bombs and privacy policy |
| 24 | Manual recurring-profile launch | FG-160 | FG-070 | SOL | pending | MAPPED | allowlist/dry-run/pause/schedule/journal tests | pending | pending | no arbitrary prompt or shell endpoint |

## Authoritative acceptance contracts

These contracts preserve the live Planning-row detail needed to audit the compact
matrix above. All dashboard work also inherits the global browser gates below.

1. Sticky page and table headers are shared across applicable views and remain
   usable with nested scrolling, keyboard navigation, small screens, and zoom.
2. Overview renders a responsive Waifu PNG only through authorized static or
   artifact IDs, with fallback art and a persistent work-safe visibility control.
3. Active Runs shows run ID, task, phase, executor type, logical agent,
   provider/model, elapsed time, and durable state; process/file inference alone is
   never reported as active.
4. Prompt Assistant is text-only, may ask clarifying questions, returns a revised
   prompt plus diff, routes only to eligible models, supports scoped enable/disable/
   ignore/restore and feedback, never claims safeguard bypass, and never submits an
   image automatically.
5. Enhance, Rewrite, and Expand preserve the original, support compare/discard,
   record the producer model, and exclude weak models only from Prompt Assistant.
6. Image work exposes submitted, queued, running, artifact-processing, completed,
   failed, elapsed, and supported cancellation states without invented GPU percent.
7. Gallery APIs and UI provide bounded pagination, thumbnails, prompt excerpt, seed,
   preset, date, status, collection, and retention-aware behavior.
8. Reuse restores prompt, negative prompt, seed, and a compatible preset and warns
   when the recorded preset version is unavailable.
9. Delete-one requires authentication, CSRF, confirmation, a fail-closed feature
   flag, path-safe artifact authorization, trash/restore retention, and an audit event;
   protected assets are rejected.
10. Delete-all is limited to the explicit current collection/filter, previews exact
    count and size, requires typed confirmation, trashes before retention deletion,
    supports restore, and separates protected/pinned assets.
11. Download-all streams a bounded ZIP of authorized artifact IDs in the current
    collection with a manifest and count, size, memory, disk, and timeout limits.
12. Folders are virtual collections with create, rename, move, and archive; masters
    remain immutable and no API accepts an arbitrary filesystem path.
13. Prompt library stores named, categorized, ordered, enabled, versioned fragments
    with reusable groups/tags, insertion toggles, composition preview, and conflict
    handling.
14. Structured prompt UI covers quality/style, medium, subject/character,
    pose/action, setting/environment, composition/camera, lighting/color, details,
    constraints/negative, preset-family templates, and raw override.
15. Structured fields remain visual metadata, deterministically compose one ordinary
    prompt string for the existing image API, store sections plus final text, and show
    the final preview before explicit submission.
16. Sequential queue has a durable batch parent, ordered children, one active GPU
    image job, pause/resume/cancel, per-item status, automatic collection, restart
    recovery, and isolated item failures.
17. The final navigation destination is Logs: a chronological combined notification
    and audit view with severity/source filters; storage remains separate and delivery
    failures stay visible.
18. Agents & Models combines logical agent/role, active provider/model, fallback,
    health, capacity, active run, recent failures, and expandable provider inventory
    without conflating logical agents with provider models.
19. Night Owl is a Dispatch execution profile with schedule, intake, current job,
    handoff artifacts, and stricter worktree, approval, and deadline policy.
20. Expandable Forge task rows show objective, phase, agents/models, progress,
    artifacts, tests, errors, audit events, and paginated bounded logs without secrets
    or full raw terminal dumps.
21. Night Owl is independent of Jira; Jira is an optional source/integration and
    existing records retain migration and display compatibility.
22. First-class work items provide title, description, status, priority, owner,
    due/schedule, labels, source links, updates, activity, dense list/detail UI,
    permissions, optimistic concurrency, and audit history without duplicating the
    execution ledger.
23. Task attachments use authorized IDs, MIME/size/checksum/quota rules, thumbnails,
    retention, generated-artifact links, malware/decompression-bomb handling, and
    privacy controls; browser paths are never trusted.
24. Manual recurring-profile controls list only approved profiles and schedules and
    offer run-now, dry-run, pause/resume, and previous/next run through normal
    journalled task creation, never arbitrary shell or free-form execution.

## Global browser acceptance gates

- All controls are keyboard reachable, semantically labelled, and show visible focus;
  disclosure controls expose expanded state and dialogs manage/restore focus.
- Tables, cards, prompts, dialogs, and sticky regions remain readable and operable on
  small screens and at browser zoom without hiding required actions.
- Navigation has durable URLs: deep links, back/forward, and refresh restore the
  selected view and fetch durable server state rather than relying on a JS variable.
- Loading, empty, stale, partial, permission-denied, and failure states are explicit;
  destructive confirmation cannot be triggered accidentally or only by pointer.
- A real-browser harness must cover these gates before a row can become `COMPLETE`;
  unit-level HTML string assertions alone are insufficient.

## Foundation and repair tasks

| Task | Owner | State | Evidence / next gate |
| --- | --- | --- | --- |
| FG-000 architecture baseline | SOL; Qwen Mini advisory rejected | COMPLETE | 171 focused, 332 full, frontend/build/parity; both Luna re-reviews PASS |
| `1bbad6a` context/compaction recovery | SOL | COMPLETE | `19dfb0c` plus `9592d46`: 234 focused and 359 full tests pass; frontend test/check/build/MCP parity, compileall, and diff checks pass; both independent Luna High reviews PASS on each final slice |
| Qwen Autopilot reliability | SOL | REVIEW | isolated commits `5f218e6`, `6eb637f`; package verifier passes 53 tests plus checksum/compile/shell/manifest gates; install is held for required Luna reviews |
| Terminal evidence/session polling | SOL | REVIEW | working tree: exact Open Terminal ID/offset polling, terminal-only evidence, durable active process, bounded redacted dashboard state; 25 focused tests pass |
| Model Monitor | Qwen Code after FG-020 | MAPPED | recovered handoff is design input to FG-060, not a separate screen |
| Open Terminal installer | SOL audit only | MAPPED | v0.13-v0.15 STOP defects resolved by preserved v0.16 evidence; no deployment authorized |
| R-001 lifecycle/journal reconciliation | SOL | MAPPED | crash consistency and durable Active Runs are blocking FG-020 gates |
| R-002 writer lease/session cleanup | SOL | REVIEW | token-fenced release, exact-callback stale renewal, digest-checked restart replay, in-flight takeover rejection, deterministic process kill, and stale model-response guard implemented; Luna reviews and host HTTP rerun pending |
| R-003 cancellation/process cleanup | SOL | IN_PROGRESS | Developer cancellation now keeps its fence through exact process kill; image side effects and Night Owl process groups remain |
| R-004 personal API hardening | SOL | MAPPED | bounded bodies, constant-time bearer validation, and explicit loopback/Docker-bridge authorization tests |
| R-005 artifact integrity | SOL | MAPPED | atomic no-overwrite masters, cleanup, digest verification; blocks FG-040/120/140/150/170/190 |
| R-006 scheduler integrity | SOL | MAPPED | renew/cover manual leases, reject malformed success, and make ambiguous submission outcomes idempotent; blocks FG-160 |
| R-007 schema migration/feature flags | SOL | MAPPED | fixture upgrade, documented rollback, fail-closed destructive defaults; blocks schema/destructive work |
| R-008 browser acceptance harness | Qwen Code, SOL verification | MAPPED | keyboard/focus/mobile/history/refresh gates; blocks every dashboard row from COMPLETE |
| R-009 packaging/version alignment | SOL | MAPPED | installer covers approved dashboard/personal/scheduler/MCP units; one package version source; blocks deployment package |

## Independent FG-000 review findings

Both Lunas reviewed the same unmodified draft without seeing the other's result.
`RESOLVED-IN-BASELINE` means the defect is now accurately recorded and assigned;
it does not mean the implementation defect itself is fixed.

| Finding | Reviewer | Result | Resolution / owned gate |
| --- | --- | --- | --- |
| LA-001 cross-store lifecycle is non-atomic | Luna Architecture | RESOLVED-IN-BASELINE | corrected single-ledger wording; R-001 / FG-020 |
| LA-002 expired Developer writer leases are not fenced | Luna Architecture | RESOLVED-IN-BASELINE | R-002 |
| LA-003 cancellation permits later side effects | Luna Architecture | RESOLVED-IN-BASELINE | R-003 |
| LA-004 Night Owl timeout leaves process descendants | Luna Architecture | RESOLVED-IN-BASELINE | R-003 |
| LA-005 personal request body is unbounded and bridge bind was omitted | Luna Architecture | RESOLVED-IN-BASELINE | boundary corrected; R-004 |
| LA-006 artifact immutability/integrity was overstated | Luna Architecture | RESOLVED-IN-BASELINE | claim corrected; R-005 |
| LA-007 scheduler lease/result integrity gaps | Luna Architecture | RESOLVED-IN-BASELINE | R-006 |
| LA-008 catalog context omitted by generic callers | Luna Architecture | RESOLVED-IN-BASELINE | explicit `1bbad6a` repair gate |
| LA-009 ComfyUI progress percentage is fabricated | Luna Architecture | RESOLVED-IN-BASELINE | FG-030 acceptance explicitly forbids it |
| LA-010 migration/feature-flag gate was not owned | Luna Architecture | RESOLVED-IN-BASELINE | R-007 blocks dependent rows |
| LP-001 accessibility/mobile/history/refresh gates absent | Luna Product | RESOLVED-IN-BASELINE | global browser gates plus R-008 |
| LP-002 read-only review could not rerun write/socket tests | Luna Product | ENVIRONMENT-CAVEAT | commands/results retained; writable host rerun required |
| LP-003 row shorthand lacked auditable acceptance detail | Luna Product | RESOLVED-IN-BASELINE | 24 authoritative acceptance contracts added |
| LA-R2-001 scheduler ambiguous submission/manual lease gap | Luna Architecture | RESOLVED-IN-BASELINE | R-006 expanded with idempotency and manual-run fencing |
| LA-R2-002 installer/version gaps lacked an owner | Luna Architecture | RESOLVED-IN-BASELINE | R-009 added as deployment blocker |
| LA-R2-003 personal token equality was not in security gate | Luna Architecture | RESOLVED-IN-BASELINE | R-004 expanded with constant-time validation |
| LA-R2-004 matrix described itself as already committed | Luna Architecture | RESOLVED-IN-BASELINE | wording corrected to durable checkpoint |

Final re-review: Luna Product returned PASS with no remaining findings; Luna
Architecture returned PASS after the four second-round corrections, with no remaining
findings or new critical contradiction.

## Independent context-compaction repair findings

Architecture and Product reviewed identical snapshots independently. SOL corrected
each reproducible STOP finding before requesting another review. The final snapshot
at `19dfb0c` received PASS from both Luna High reviewers and the original read-only
context auditor.

| Finding | Disposition |
| --- | --- |
| content markers could spoof handoff provenance | fixed with caller-owned provenance buckets and a true-client-user index sidecar |
| retries could displace the original objective | fixed by separate server-control context; both messages are required under compaction |
| malformed or duplicate tool groups could leave half-groups | fixed by one sequential atomic parser and global normalized-ID reservation |
| newest terminal evidence could be discarded | fixed by atomic retention and bounded in-place head/tail summaries |
| list-shaped objectives or tool results could change shape or disappear | fixed with text extraction for durable objectives and structure-preserving part compaction |
| contentless user/assistant messages were accepted | fixed by boundary validation |
| converted client-system messages could replace the objective or stringify parts | fixed by positional provenance and structure-preserving untrusted wrappers |
| structured status evidence was alleged to become `unknown` | not reproduced; existing status fallback classified nested/list exit codes and now has an explicit regression assertion |

## Independent context-metadata repair findings

Architecture and Product reviewed the same final uncommitted snapshot independently.
Its SHA-256 was
`463c638adda8925b339aee5ce0b5d2b7ef68f4dab10bc948edb9652761d10fd3` before
staging and after each review. Both reviewers returned PASS; the accepted snapshot is
commit `9592d46`.

| Finding | Disposition |
| --- | --- |
| malformed, fractional, infinite, Boolean, or oversized context values could be truncated or crash SQLite | fixed with signed-64-bit integer-only validation at metadata and explicit-input boundaries |
| conflicting metadata could select an unsafe larger context | fixed by consuming all supported metadata locations and choosing the smallest valid positive value |
| a fallback context was stored as authoritative and prevented later runtime expansion | fixed by persisting unknown metadata as NULL while preserving runtime fallback resolution |
| catalog refresh could silently expand an established model limit | fixed so refreshes tighten known limits but never expand them; explicit operator updates remain available |
| HTTP status-only 413/429 failures lost their distinct meanings | fixed so 413 is `context_overflow`, 429/503 is `capacity`, and authentication remains higher priority |
| orchestration and Personal API wrappers erased uniform upstream failure categories | fixed by preserving a common category and keeping mixed failures generic |
| context-overflow probes and attempts falsely degraded health, reliability, and cooldown | fixed by retaining audit history while excluding context-fit failures from provider health signals |
| CLI, dashboard, orchestrator, benchmark, judge, and Personal calls omitted catalog context | fixed at every completion call site with focused propagation tests |

## Significant decisions and corrections

1. The live Planning tab, not the Autopilot manifest status, defines the 24 product
   requirements. The manifest's 21 dependency tasks are retained as IDs and planning
   input.
2. `TaskJournal` remains the intended canonical execution ledger. This durable matrix
   is an engineering checkpoint, not a second task-journal subsystem; the live catalog
   is not mutated for program tracking.
3. Qwen Autopilot remains paused. Its timer is disabled, its database is backed up,
   and SOL holds its process lock until the write phase ends.
4. Autopilot recovery is frozen in the disposable `sol/autopilot-repair` branch at
   `6eb637f` (aggregate diff SHA-256
   `c841c63a32f593cfe62a34346168fc3f61a026606c50606be695e8539518f043`).
   Its verifier passes 53 tests plus checksum, compile, shell, and manifest gates;
   deliberate checksum tampering fails. The installed package remains untouched until
   independent Luna Architecture and Product reviews pass.
5. `1bbad6a` is retained, not reverted. Commit `19dfb0c` completes its
   compaction/normalization repair slice; `9592d46` completes conservative context
   metadata, caller propagation, and neutral context-overflow handling. Both slices
   passed separate independent Architecture and Product reviews.
6. The five previously unreachable Qwen context commits are preserved by local
   `refs/archive/qwen-context/*`; no recovered material was deleted.
7. Model Monitor research will extend Agents & Models. It will not create another
   inventory or dashboard destination.
8. Open Terminal v0.16 resolved the historical installer STOP defects. The recorded
   missing Windows ComfyUI tunnel is an external deployment-acceptance dependency,
   not authority to change live services during this program.
9. Push, deployment, service restarts, live database changes, firewall changes, sudo,
   and production migrations remain prohibited without later authorization.
10. Qwen Mini was run twice read-only. Both responses were rejected: the first was
    empty and the narrowed retry invented nonexistent `src/components/*` paths. Qwen
    output remains advisory and repository evidence controls.
11. Independent Luna Product and Architecture reviews both passed the corrected
    FG-000 baseline. Their STOP findings remain recorded above as implementation
    gates rather than being erased after correction.
12. The required Autopilot Luna reviews could not start because the external Luna
    usage gate is exhausted until 2026-08-08. No substitute model is being presented
    as Luna; implementation continues while this review gate remains open.

## Current rollover checkpoint

- Branch: `feature/swarm-developer`; accepted HEAD `d3a0c14`; R-002 implementation is
  in the working tree pending its local commit and independent review.
- Active task: R-002 terminal evidence, writer fencing, pending-session cleanup, and
  cancellation race closure.
- Exact next action: finish SOL diff review, run every non-sandboxed verification
  available, create the narrow local implementation commit, then hold R-002 at
  `REVIEW` until both required Luna reviews and the socket-based HTTP rerun are
  available.
- Open findings: Autopilot and R-002 await both Luna PASS results; the account's
  external-model/host-execution gate is exhausted until 2026-08-08; provider-qualified
  model identity remains a later Agents & Models migration concern.
- Verified context recovery: 234 focused and 359 full Python tests; frontend
  test/check/build/MCP parity; compileall and `git diff --check` all passed. The bare
  `python -m unittest` command discovers zero repository tests, so the recorded full
  command is `python -m unittest discover -s tests`.
- Pending external repair commits: Autopilot `5f218e6` and `6eb637f` remain isolated
  and are not installed or represented as reviewed.
- R-002 evidence: `python3 -m unittest tests.test_developer.DeveloperCoordinatorTest
  -v` passed 25/25; compileall and `git diff --check` passed. Full discovery ran 362
  tests: 333 passed and 29 socket-dependent tests errored uniformly with sandbox
  `PermissionError`. The required host rerun was requested and rejected by the same
  exhausted external-usage gate, so no HTTP/browser PASS is claimed.
