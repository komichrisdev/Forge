# Swarm Platform V2 rollback

This runbook restores the verified Phase 0.2 backup without changing Plex,
qBittorrent, Docker, Open WebUI, UFW, or system-level startup. Run it only
after an explicit rollback decision.

Verified backup used by this runbook:

```text
/home/komichris/backups/owui-swarm/20260725T205641Z
```

## 1. Preconditions

Log in as `komichris`. Do not use `sudo` for Swarm files.

```bash
backup_root=/home/komichris/backups/owui-swarm/20260725T205641Z
source_root=/home/komichris/openwebui-codex-swarm
runtime_root=/home/komichris/.local/share/owui-swarm
config_root=/home/komichris/.config/owui-swarm
restore_stage=/home/komichris/backups/owui-swarm/rollback-stage-$(date -u +%Y%m%dT%H%M%SZ)

test -d "$backup_root"
test "$(stat -c %a "$backup_root")" = 700
(cd "$backup_root" && sha256sum -c SHA256SUMS)
install -d -m 700 "$restore_stage"
tar -xzf "$backup_root/source/openwebui-codex-swarm.tar.gz" -C "$restore_stage"
test -f "$restore_stage/openwebui-codex-swarm/pyproject.toml"
test -f "$restore_stage/openwebui-codex-swarm/chatgpt_app/build/main.js"
```

`configuration/` is intentionally omitted from `SHA256SUMS` because it
contains secret-bearing files. Verify only its ownership and modes:

```bash
find "$backup_root/configuration" -printf '%p %u:%g %m\n'
```

Do not display configuration contents.

## 2. Stop only Swarm user services

Record the current state, then stop the two Swarm services:

```bash
systemctl --user status --no-pager owui-swarm-dashboard.service owui-swarm-chatgpt-app.service
systemctl --user stop owui-swarm-dashboard.service owui-swarm-chatgpt-app.service
systemctl --user is-active owui-swarm-dashboard.service owui-swarm-chatgpt-app.service
```

Do not stop or restart Docker, Open WebUI, Plex, qBittorrent, UFW, or any
system service.

## 3. Preserve the state being replaced

Create a restricted recovery copy before replacing anything:

```bash
pre_rollback="$restore_stage/replaced-state"
install -d -m 700 "$pre_rollback"
tar -czf "$pre_rollback/source.tar.gz" \
  --exclude='.git' --exclude='.venv' --exclude='chatgpt_app/node_modules' \
  -C "$(dirname "$source_root")" "$(basename "$source_root")"
cp -a /home/komichris/.local/bin/owui-swarm "$pre_rollback/owui-swarm"
cp -a "$runtime_root/catalog.sqlite3" "$pre_rollback/catalog.sqlite3"
cp -a "$config_root" "$pre_rollback/configuration"
chmod -R go-rwx "$pre_rollback"
```

## 4. Restore source files and the compiled MCP build

The source snapshot contains the exact pre-Git source, including
`chatgpt_app/build/` and `chatgpt_app/dist/`. It excludes the reproducible
Python virtual environment and `node_modules`; preserve those installed
dependency directories during rollback.

```bash
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='chatgpt_app/node_modules/' \
  "$restore_stage/openwebui-codex-swarm/" "$source_root/"

test -f "$source_root/chatgpt_app/build/main.js"
test -f "$source_root/chatgpt_app/build/server.js"
test -f "$source_root/chatgpt_app/build/data.js"
cmp "$restore_stage/openwebui-codex-swarm/chatgpt_app/build/main.js" \
  "$source_root/chatgpt_app/build/main.js"
```

Do not rebuild the Node application during rollback.

## 5. Restore the executable wrapper

```bash
install -m 755 "$backup_root/executable/owui-swarm" \
  /home/komichris/.local/bin/owui-swarm
test "$(readlink -f /home/komichris/.local/bin/owui-swarm)" = \
  /home/komichris/.local/bin/owui-swarm
```

The wrapper should continue to execute
`/home/komichris/openwebui-codex-swarm/.venv/bin/owui-swarm`.

## 6. Restore runtime SQLite and private state

With both Swarm services stopped, restore the catalog through SQLite's backup
API into a temporary file and atomically replace the destination:

```bash
python3 -c 'import os,sqlite3,sys
src,dst=sys.argv[1:3]
tmp=dst+".rollback-new"
os.path.exists(tmp) and os.unlink(tmp)
s=sqlite3.connect("file:"+src+"?mode=ro",uri=True)
d=sqlite3.connect(tmp)
s.backup(d)
d.close()
s.close()
os.chmod(tmp,0o600)
os.replace(tmp,dst)' \
  "$backup_root/runtime/catalog.sqlite3" "$runtime_root/catalog.sqlite3"

python3 -c 'import sqlite3,sys
c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True)
assert c.execute("pragma integrity_check").fetchone()[0]=="ok"
c.close()' "$runtime_root/catalog.sqlite3"
```

Merge the preserved pre-baseline private runs and benchmark evidence without
deleting any newer artifacts:

```bash
rsync -a "$backup_root/runtime/private-state/runs/" "$runtime_root/runs/"
rsync -a "$backup_root/runtime/private-state/benchmarks/" "$runtime_root/benchmarks/"
```

Never display run, prompt, worker, judge, or benchmark contents.

## 7. Restore configuration and permissions

The configuration copy may contain credentials. Preserve the current copy,
restore with metadata, and verify modes without reading values:

```bash
rsync -a --delete "$backup_root/configuration/owui-swarm/" "$config_root/"
find "$config_root" -printf '%p %u:%g %m\n'
test "$(stat -c %a "$config_root/environment")" = 600
test "$(stat -c %a "$config_root/config.toml")" = 600
```

Do not print, diff, or hash the secret-bearing files.

## 8. Start and verify only Swarm

```bash
systemctl --user start owui-swarm-chatgpt-app.service owui-swarm-dashboard.service
systemctl --user is-active owui-swarm-dashboard.service owui-swarm-chatgpt-app.service
ss -tlnp | grep -E '127\.0\.0\.1:(8787|8790)\b'
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/api/models
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/health
```

Expected results are `active`, loopback listeners on `8787` and `8790`, and
HTTP `200`. Open WebUI must remain on its existing port and must not be
recreated.

Confirm that the Open WebUI container start timestamp and ID match the
metadata in the backup before considering the rollback complete.

## 9. Abandon the feature branch

Only after preserving any wanted work:

```bash
cd /home/komichris/openwebui-codex-swarm
git status --short
git switch main
git branch -D feat/swarm-platform-v2
```

Do not use `git reset --hard` to discard unreviewed work.

## 10. Return to the pre-Git state

Only if the user explicitly chooses to remove project-local Git metadata,
move it to restricted recovery storage:

```bash
cd /home/komichris/openwebui-codex-swarm
test "$(git rev-parse --show-toplevel)" = \
  /home/komichris/openwebui-codex-swarm
git_metadata_backup=/home/komichris/backups/owui-swarm/project-git-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 700 "$git_metadata_backup"
mv .git "$git_metadata_backup/.git"
git rev-parse --show-toplevel
```

The final command should report that the directory is not a Git repository.
Do not alter `/home/komichris/.git`; it was already an invalid, empty parent
directory before Phase 0.2.

After the rollback is accepted, the temporary rollback stage may be removed
only with a separate, explicit cleanup decision. Keep the verified backup.
