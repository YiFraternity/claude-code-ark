#!/usr/bin/env bash
# Install the tracked Claude Ark launcher for the current user.
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
config_dir="${CLAUDE_ARK_CONFIG_DIR:-$config_root/claude-ark}"
map_path="${CLAUDE_ARK_MODEL_MAP:-$config_dir/ak_map_available.json}"
force=0
skip_dependencies=0

usage() {
  cat <<'EOF'
Usage: bash install_claude_ark.sh [--force] [--skip-dependencies]

Installs the claude-ark command for the current user and creates a local model
map. `--force` replaces an existing claude-ark command that points elsewhere.
`--skip-dependencies` is intended only when LiteLLM is already installed in the
selected Python environment.
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --force) force=1 ;;
    --skip-dependencies) skip_dependencies=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "${CLAUDE_ARK_PYTHON:-}" ]]; then
  python_bin="$CLAUDE_ARK_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
else
  printf 'claude-ark install: Python 3 is required; set CLAUDE_ARK_PYTHON if needed\n' >&2
  exit 1
fi

if [[ "$skip_dependencies" -eq 0 ]]; then
  "$python_bin" -m pip install --upgrade 'litellm[proxy]' PyYAML
fi

install -d -m 700 "$config_dir"
if [[ ! -e "$map_path" ]]; then
  source_map="${CLAUDE_ARK_MODEL_MAP_SOURCE:-}"
  if [[ -z "$source_map" ]]; then
    candidate_map="$(cd "$source_dir/../.." && pwd)/ak_map_available.json"
    [[ -f "$candidate_map" ]] && source_map="$candidate_map"
  fi
  if [[ -n "$source_map" && -f "$source_map" ]]; then
    install -m 600 "$source_map" "$map_path"
    printf 'claude-ark install: copied an existing local model map into the user configuration directory\n'
  else
    install -m 600 "$source_dir/ak_map_available.example.json" "$map_path"
    printf 'claude-ark install: created a model-map template; fill its api_keys before first use\n'
  fi
fi

target_dir="$HOME/.local/bin"
target="$target_dir/claude-ark"
install -d -m 700 "$target_dir"
if [[ -e "$target" || -L "$target" ]]; then
  current_target="$(readlink -f "$target" 2>/dev/null || true)"
  wanted_target="$(readlink -f "$source_dir/claude-ark")"
  if [[ "$current_target" != "$wanted_target" && "$force" -ne 1 ]]; then
    printf 'claude-ark install: %s already exists; rerun with --force to replace it\n' "$target" >&2
    exit 2
  fi
  rm -f "$target"
fi
chmod 700 "$source_dir/claude-ark"
ln -s "$source_dir/claude-ark" "$target"

if ! command -v claude >/dev/null 2>&1; then
  printf 'claude-ark install: Claude Code CLI is not on PATH; install it or set CLAUDE_BIN before running claude-ark\n' >&2
fi
printf 'claude-ark install: ready. Run claude-ark after confirming the local model map contains your keys.\n'
