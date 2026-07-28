#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
state_dir=${NIGHT_OWL_STATE_DIR:-"$HOME/.local/share/owui-swarm/night-owl"}
codex_bin=${NIGHT_OWL_CODEX:-/usr/local/bin/codex}
run_hours=${NIGHT_OWL_RUN_HOURS:-4}
run_limit=${NIGHT_OWL_TIMEOUT:-${run_hours}h}
project=${NIGHT_OWL_JIRA_PROJECT:-KAN}

[[ $run_hours =~ ^[1-9][0-9]*$ ]] || { echo "NIGHT_OWL_RUN_HOURS must be a positive integer" >&2; exit 1; }

mkdir -p "$state_dir"
exec 9>"$state_dir/night-owl.lock"
flock -n 9 || exit 0

for command in flock timeout "$codex_bin"; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

if [[ ${1:-} == --dry-run ]]; then
  echo "Night Owl ready: repo=$repo_dir state=$state_dir limit=$run_limit"
  exit 0
fi

run_id=$(date -u +%Y%m%dT%H%M%SZ)
log="$state_dir/$run_id.jsonl"
last_message="$state_dir/last-message.txt"
queue_snapshot="$state_dir/$run_id-jira-queue.json"
deadline=$(date -d "+$run_hours hours" --iso-8601=seconds)
: >"$last_message"

set +e
PYTHONPATH="$repo_dir" python3 -m swarm_router.night_owl_jira preflight --project "$project" --output "$queue_snapshot" >"$log" 2>&1
preflight_status=$?
set -e
if (( preflight_status != 0 )); then
  cat >"$last_message" <<EOF
Night Owl stopped before work: Jira REST preflight failed.
Snapshot: $queue_snapshot
EOF
  cat >>"$state_dir/report.md" <<EOF
- Automation failed: Jira REST preflight failed. Snapshot: $queue_snapshot
EOF
  exit "$preflight_status"
fi

issue_count=$(python3 - "$queue_snapshot" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("issue_count", 0))
PY
)
if [[ "$issue_count" == "0" ]]; then
  cat >"$last_message" <<EOF
Night Owl queue verified empty via Jira REST.
Snapshot: $queue_snapshot
EOF
  echo "Night Owl queue verified empty via Jira REST: snapshot=$queue_snapshot"
  exit 0
fi

set +e
timeout --signal=TERM --kill-after=5m "$run_limit" \
  "$codex_bin" -a never exec --skip-git-repo-check -C "$HOME" \
  -m gpt-5.4-mini -c 'model_reasoning_effort="medium"' \
  -s workspace-write --json -o "$last_message" \
  "Use \$night-owl with \$codex-colage to process the eligible Jira queue sequentially. The KomiChris Atlassian MCP cloud is not granted in this Codex session; do not use atlassian_rovo for KomiChris Jira. Use Jira REST credentials from ~/.config/night-owl/env and the verified queue snapshot at $queue_snapshot. Process only issues in that snapshot. Do not send Discord directly; leave report artifacts in $state_dir/report.md for Forge delivery. Stop before $deadline so Jira and GitHub handoffs finish on time, and end the final response with NIGHT_OWL_REST_QUEUE_VERIFIED." \
  >>"$log" 2>&1
status=$?
set -e

if (( status == 0 )) && ! grep -q "NIGHT_OWL_REST_QUEUE_VERIFIED" "$last_message"; then
  status=1
  echo '- Automation failed: live Jira REST queue was not verified in final response.' >>"$state_dir/report.md"
elif (( status != 0 )); then
  cat >>"$state_dir/report.md" <<EOF
- Automation failed with exit code $status.
- Log: $log
EOF
fi

exit "$status"
