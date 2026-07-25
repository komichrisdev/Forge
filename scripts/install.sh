#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! python3 -m venv "$ROOT/.venv"; then
  command -v uv >/dev/null || {
    echo "Install python3-venv or uv, then rerun this installer." >&2
    exit 1
  }
  uv venv --seed --allow-existing --python /usr/bin/python3 "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/owui-swarm"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
  cp "$ROOT/config.example.toml" "$CONFIG_DIR/config.toml"
  echo "Created $CONFIG_DIR/config.toml"
fi
if [[ ! -f "$CONFIG_DIR/environment" ]]; then
  install -m 600 /dev/null "$CONFIG_DIR/environment"
fi
chmod 600 "$CONFIG_DIR/environment"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat >"$BIN_DIR/owui-swarm" <<EOF
#!/usr/bin/env bash
set -a
[[ ! -f "$CONFIG_DIR/environment" ]] || source "$CONFIG_DIR/environment"
set +a
exec "$ROOT/.venv/bin/owui-swarm" "\$@"
EOF
chmod 755 "$BIN_DIR/owui-swarm"

SKILL_DIR="$HOME/.agents/skills"
mkdir -p "$SKILL_DIR"
if [[ -e "$SKILL_DIR/openwebui-swarm" || -L "$SKILL_DIR/openwebui-swarm" ]]; then
  if [[ "$(readlink -f "$SKILL_DIR/openwebui-swarm")" != "$(readlink -f "$ROOT/.agents/skills/openwebui-swarm")" ]]; then
    echo "Refusing to overwrite existing skill: $SKILL_DIR/openwebui-swarm" >&2
    exit 1
  fi
else
  ln -s "$ROOT/.agents/skills/openwebui-swarm" "$SKILL_DIR/openwebui-swarm"
fi

SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
install -m 644 "$ROOT/systemd/owui-swarm-dashboard.service" \
  "$SYSTEMD_DIR/owui-swarm-dashboard.service"

cat <<EOF
Installed.

Next:
1. Edit $CONFIG_DIR/config.toml
2. Put OPEN_WEBUI_API_KEY=... in $CONFIG_DIR/environment
3. Ensure $BIN_DIR is in PATH
4. Run: owui-swarm doctor
EOF
