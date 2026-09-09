# Claude Ark

Portable deployment for running Claude Code through a local LiteLLM gateway
with Ark and ModelHub model routes.

## Install

Clone this repository on the target development machine, activate the intended
Python environment (for example, conda `base`), and run:

```bash
bash install_claude_ark.sh
```

The installer installs LiteLLM and PyYAML, creates a user-only model-map file,
and exposes `claude-ark` under `~/.local/bin`. It never writes provider keys
into this checkout.

On a fresh machine the model map is created from
`ak_map_available.example.json`. Fill each required `api_keys` entry in the
local file before use, then start Claude Code:

```bash
claude-ark
```

The default model is `gpt-5.5-2026-04-24`. Use `claude-ark --select-model` to
choose another configured model, or `claude-ark --model <name>` for one launch.

For VS Code, configure the Claude Code extension's process wrapper to use the
installed `claude-ark` command, then start a new Claude session.

## ModelHub tool compatibility

The ModelHub proxy preserves Claude Code tool calls and tool results. It also
removes `pattern` constraints only from tool parameter schemas: Claude Code's
`Artifact` tool contains a Unicode regex that ModelHub cannot parse. Types,
required fields, and all other tool structure are retained.

## Botmux

Use `botmux.example.json` as a field reference when adding a Botmux robot. It
keeps Botmux on the `claude-code` adapter while making `claude-ark` the launch
wrapper. Replace every angle-bracket placeholder locally; the example must
remain credential-free.

For an existing Botmux robot, set only the adapter and wrapper fields, then
restart Botmux:

```bash
botmux setup edit <process-name-or-app-id> --cli claude-code --wrapper-cli claude-ark
botmux restart
```

Existing Claude sessions retain their current process. Start a new topic after
the restart to use the `claude-ark` wrapper.

## Build a standalone archive

```bash
bash package_claude_ark.sh
```

The archive is built from an explicit whitelist: launcher, proxies, registry,
selector, installer, README, and the empty-key model template. It excludes
real model maps, `.env`, generated LiteLLM configuration, logs, and caches.

On a target machine:

```bash
tar -xzf claude-ark-portable.tar.gz
cd claude-ark
bash install_claude_ark.sh
```

## Verify

```bash
python3 test_portable_bundle.py
```
