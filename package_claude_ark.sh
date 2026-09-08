#!/usr/bin/env bash
# Build a standalone, credential-free Claude Ark archive from tracked sources.
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_path="${1:-$PWD/claude-ark-portable.tar.gz}"
bundle_name="claude-ark"
files=(
  claude-ark
  install_claude_ark.sh
  package_claude_ark.sh
  ark_compat_proxy.py
  modelhub_compat_proxy.py
  claude_ark_registry.py
  claude_ark_model_selector.py
  ak_map_available.example.json
  botmux.example.json
  README.md
)

for file in "${files[@]}"; do
  [[ -f "$source_dir/$file" ]] || {
    printf 'claude-ark package: required source is missing: %s\n' "$file" >&2
    exit 1
  }
done

output_dir="$(dirname -- "$output_path")"
install -d "$output_dir"
tar -czf "$output_path" --transform "s,^,$bundle_name/," -C "$source_dir" "${files[@]}"
printf 'claude-ark package: created %s\n' "$output_path"
