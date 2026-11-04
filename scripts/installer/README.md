# MCP Core - Installation & Usage Guide

![python-badge](https://img.shields.io/badge/Python-3.11--3.14-green)

MCP Core is the shared foundation of the NXP MCP server suite. This package installs:

| Package | Short name | Description |
|---|---|---|
| `nxp-mcp-shared` | _(always installed)_ | Shared infrastructure used by every NXP MCP server (action framework, config, bootstrap, JSON-RPC client) |
| `nxp-mcp-gateway` | `gateway` | Gateway aggregator - mounts every installed NXP MCP server behind one MCP endpoint |
| `nxp-mcp-knowledge` | `knowledge` | Local knowledge base server with semantic search (LanceDB + local embeddings) |

It also installs the gateway / knowledge configs, the `run-mcp.bat` / `run-mcp.sh` launchers, the agent-registration helper `register_agents.py` and the release updater `scripts/github_update_toolkit.py`.

> **Note** MCP Core ships no tool servers, no skills, no embedding model and no prebuilt knowledge index. Tool servers (S32 Design Studio, S32 Flash Tool, FreeMASTER Lite, ...) are released from their own repositories; once installed into the same environment, the gateway discovers them automatically through the `nxp.mcp.servers` entry-point group.

## 1. Prerequisites

| Prerequisite | Version | Link |
|--------------|---------|------|
| Python | 3.11 - 3.14 | [Download](https://www.python.org/downloads/) |
| MCP compatible agent | latest | For example [Claude Desktop](https://claude.com/download), [VS Code](https://code.visualstudio.com/), [Cursor](https://cursor.com/download) or [Goose](https://goose-docs.ai/docs/getting-started/installation) |

## 2. Installation

Open a terminal and run the self-installing package:

```shell
# Interactive mode (destination prompt, package selection, agent registration)
python mcp-core-installer-<version>.pyz
```

### Installer Options

| Option | Short | Description |
|--------|-------|-------------|
| `--dest DIR` | `-d` | Installation directory. Default: `C:\NXP\mcp-core` on Windows, `/usr/local/NXP/mcp-core` on Linux/macOS. Without `--dest` the installer prompts for it |
| `--yes` | `-y` | Non-interactive: install all packages without prompting. Agent registration is skipped unless `--register` is also given |
| `--packages PKG,...` | `-p` | Comma-separated list of packages to install (`gateway`, `knowledge`). `nxp-mcp-shared` is always installed |
| `--register AGENT,...` | | Register the gateway into the listed AI agents after install (`claude-desktop`, `vero`, `vscode`, `cursor`, `goose`). Use `none` to skip; omit to show an interactive menu |
| `--copy-skills [DIR]` | | Copy the skills found under `<dest>/skills` after registration. MCP Core ships none, so this step is skipped automatically until skills are added to the installation |
| `--skip-copy-skills` | | Do not copy skills |
| `--no-venv` | | Install the wheels into the system Python instead of an isolated virtual environment |

Examples:

```shell
# Install everything into a custom folder, no prompts
python mcp-core-installer-<version>.pyz --dest C:\NXP\mcp-core --yes

# Install only the gateway and register it with Claude Desktop
python mcp-core-installer-<version>.pyz --packages gateway --register claude-desktop
```

Packages that are not selected are marked `disabled: true` in the installed `configs/gateway.stdio.yaml` and `configs/gateway.http.yaml`.

> **Note (virtual environment by default):** The installer creates an isolated
> `.venv` inside the destination directory and installs the wheels into it.
> The `run-mcp.sh` / `run-mcp.bat` wrappers auto-activate this `.venv` at run
> time. This is required on PEP 668 "externally-managed" Linux distributions
> (Ubuntu 24.04, Debian 12+, Fedora), where a plain system-wide `pip install`
> fails with `error: externally-managed-environment`.
>
> The `venv` module must be available; on Debian/Ubuntu install it with
> `sudo apt install python3-venv` (matching your Python version, e.g.
> `python3.12-venv`). Pass `--no-venv` to install into the system Python
> instead (not recommended on externally-managed distributions).

> **Note:** The commands in the following sections are run from the installation directory. `<venv-python>` stands for the interpreter of the installation's virtual environment: `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on Linux/macOS (or plain `python` after a `--no-venv` install). `register_agents.py` and the updater need the packages installed in that environment.

### Installation layout

```
<dest>/
  .venv/                         virtual environment (unless --no-venv)
  configs/                       gateway.stdio.yaml, gateway.http.yaml, knowledge.standalone.yaml
  scripts/github_update_toolkit.py
  register_agents.py
  run-mcp.bat / run-mcp.sh
  README.md, LICENSE, LICENSE_ADDENDUM, SBOM.spdx.json
```

`models/` (embedding model cache), `knowledge_db/` (knowledge index), `skills/` and `.tmp/logs/` are created later, when they are first used or delivered.

## 3. Registering the gateway into AI Agents

The gateway can be registered in any of the following agents so they launch it automatically:

 * [Claude Desktop](https://claude.com/download) (`claude-desktop`)
 * Vero (`vero`)
 * [VS Code](https://code.visualstudio.com/) (`vscode`)
 * [Cursor](https://cursor.com/download) (`cursor`)
 * [Goose](https://goose-docs.ai/) (`goose`)

How each agent is registered:

- **Config-based agents** (Claude Desktop, Vero, Goose): an MCP server entry is merged into the agent's config file (created if missing). Other entries are left untouched.
- **CLI-based agents** (VS Code): `code --add-mcp` is invoked so VS Code performs the merge itself.
- **Deeplink agents** (Cursor): the agent's own install dialog opens with the gateway pre-filled.

The registered command is derived from the installation directory: `run-mcp.bat` (Windows) or `run-mcp.sh` (Linux/macOS) with the arguments `nxp.mcp.gateway <dest>/configs/gateway.stdio.yaml`. The default server name is `nxp-mcp-gateway`.

### During installation

```shell
# Interactive menu (default when --register is omitted and --yes is not used)
python mcp-core-installer-<version>.pyz

# Register specific agents, no menu
python mcp-core-installer-<version>.pyz --register claude-desktop,vscode

# Skip registration entirely
python mcp-core-installer-<version>.pyz --register none
```

### After installation

`register_agents.py` in the installation directory registers (or re-registers) the gateway at any time:

```shell
usage: register_agents [-h] [--dest DEST] [--name NAME] [--register A,B,...] [--skip-register] [--yes]
                       [--copy-skills [DIR]] [--skip-copy-skills] [--list] [--dry-run]

  --dest DEST          MCP install directory containing run-mcp.bat/.sh, configs/, and skills/ (default: current directory)
  --name NAME          Server name to register in the agent configs (default: nxp-mcp-gateway)
  --register A,B,...   Comma-separated agent keys (claude-desktop, vero, vscode, cursor, goose); 'none' to skip.
                       Omit to show the interactive menu.
  --skip-register      Skip agent registration entirely (useful when only copying skills).
  --yes, -y            Do not show menus; register into all agents and/or copy skills.
  --copy-skills [DIR]  Copy the skills under <dest>/skills. With DIR, copy them all there; otherwise show a per-agent menu.
  --skip-copy-skills   Skip the skills copy step entirely.
  --list               List all supported agents and their registration target, then exit.
  --dry-run            Show the gateway command and the per-agent target/action without writing anything.
```

Examples:

```shell
# Show what would be written, without changing anything
<venv-python> register_agents.py --dry-run

# Register into specific agents; the installation has no skills yet, so skip the copy step
<venv-python> register_agents.py --register claude-desktop,vscode --skip-copy-skills

# Register into every supported agent, no menu
<venv-python> register_agents.py --yes --skip-copy-skills
```

### Manual registration

Add an MCP server entry pointing to the gateway launcher of your installation:

```json
{
  "mcpServers": {
    "nxp-mcp-gateway": {
      "command": "C:/NXP/mcp-core/run-mcp.bat",
      "args": ["nxp.mcp.gateway", "C:/NXP/mcp-core/configs/gateway.stdio.yaml"]
    }
  }
}
```

> **Note:** Some agents (e.g. Goose) need to be restarted after registration.

## 4. Skills

MCP Core ships no skills. Skills are markdown documents (`SKILL.md`) that describe how an agent should use a server; they are delivered by tool server releases or by the updater into `<dest>/skills/`.

Every server exposes its skills as MCP resources automatically. For agents with native skill support, copy them into the agent's skills folder once they are present:

```shell
# Per-agent menu (Claude: ~/.claude/skills, Vero: ~/.vero/skills, VS Code / Goose: ~/.agents/skills, Cursor: ~/.cursor/skills)
<venv-python> register_agents.py --skip-register --copy-skills

# Copy everything into one folder
<venv-python> register_agents.py --skip-register --copy-skills ~/.claude/skills
```

## 5. Updating

The installation includes the release updater. It compares the installed NXP MCP packages with a GitHub release and installs the release wheels and content archives (configs, skills, knowledge base):

```shell
# Compare the installation with the latest MCP Core release
<venv-python> scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core check

# Download and install the update (interactive)
<venv-python> scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core update
```

Content archives are extracted into the installation directory. Run `<venv-python> scripts/github_update_toolkit.py --help` for all commands and options.

## 6. Configuration

All paths inside the bundled configs are relative to the config file, so `../models` and `../knowledge_db` resolve to folders in the installation directory.

### Logging

Available in both gateway and standalone configs:

```yaml
logging:
  level: DEBUG                        # DEBUG | INFO | WARNING | ERROR
  timestamp_format: "%Y-%m-%d %H:%M:%S"
  format: "%(asctime)s [%(name)s] %(levelname)s %(message)s"
  file: ../.tmp/logs/gateway.log      # optional - omit to log to stderr only
```

In the gateway, `logging` settings are inherited by all mounted servers unless a per-server `logging` override is provided.

### Running the Gateway

The gateway mounts every installed NXP MCP server and exposes them through one MCP connection. Each tool is prefixed with the server name (e.g. `nxp_knowledge_kb_search`).

```shell
# stdio transport - launched by the agent as a subprocess (used by agent registration)
run-mcp.bat nxp.mcp.gateway configs/gateway.stdio.yaml

# HTTP transport - one shared, long-running instance at http://127.0.0.1:8080/mcp
run-mcp.bat nxp.mcp.gateway configs/gateway.http.yaml
```

Use `run-mcp.sh` instead of `run-mcp.bat` on Linux/macOS. The HTTP host and port are set by `http.host` / `http.port` in `configs/gateway.http.yaml`.

A gateway config has three sections:

- `logging` - shared logging settings
- `gateway` - gateway-level flags (`strict_startup`: fail if any enabled server fails to load; `skills`: extra skill directories)
- `servers` - one entry per server, with its `settings`, optional `skills` path and optional `disabled: true`

Installed tool servers do not need an entry: the gateway discovers them through the `nxp.mcp.servers` entry-point group. Add an entry under `servers` only to pass `settings` to a server or to keep an installed server from loading with `disabled: true`.

### Knowledge Base

The knowledge base can run inside the gateway or standalone:

```shell
run-mcp.bat nxp.mcp.knowledge configs/knowledge.standalone.yaml
```

Main settings (`configs/knowledge.standalone.yaml`, or `servers.knowledge.settings` in a gateway config):

| Field | Description |
|-------|-------------|
| `db_path` | Folder where LanceDB stores the index, one table per corpus (default `../knowledge_db/bge-base-en-v1.5`) |
| `corpora` | Named corpora; each has a `name` (LanceDB table) and `dirs` scanned at startup. Missing dirs are skipped with a warning |
| `embedder.model` | Embedding model, a HuggingFace id (`BAAI/bge-base-en-v1.5`) or a local path |
| `embedder.dimension` | Vector dimension; must match the model (`768` for `bge-base-en-v1.5`) |
| `embedder.cache` | Folder where the model is cached / loaded from (default `../models`) |
| `embedder.offline` | `true` = load the model from `cache` only, no network access; `false` = download it on first use |
| `mutable` | `true` enables the `kb_ingest` / `kb_remove` tools for adding your own documents |

**Embedding model:** the package does not include the model and the bundled configs use `offline: true`. Pre-download it once into the installation's `models/` folder:

```shell
<venv-python> -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-base-en-v1.5', cache_dir='models')"
```

Alternatively set `embedder.offline: false` to let the knowledge base download it on first use. The model is loaded lazily, on the first search or ingestion.

**Knowledge index:** semantic search returns results only after an index exists under `db_path` - either a prebuilt index delivered by a release (see [Updating](#5-updating)) or your own documents ingested with `mutable: true`.
