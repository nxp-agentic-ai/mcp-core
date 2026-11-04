# MCP Core

![python-badge](https://img.shields.io/badge/Python-3.11--3.14-green)
![fastmcp-badge](https://img.shields.io/badge/FastMCP-4.x-orange?logo=fastmcp)

The shared foundation of the NXP MCP server suite. MCP Core provides the root **gateway** that aggregates installed servers behind a single MCP connection, a local **knowledge base** server for semantic search over NXP documentation, and the **shared infrastructure** (the search-and-execute action framework, response/error standardization, OpenRPC synthesis, JSON-RPC client, config and bootstrap) that every NXP MCP tool server is built on.

## Features

- Gateway composition - all servers accessible through a single MCP connection
- Dynamic server discovery via the `nxp.mcp.servers` entry-points - installed servers are mounted with no gateway code change
- Namespace-prefixed tool mounting (`nxp_<name>`) to avoid naming conflicts
- Opt-out mounting semantics (`disabled: true` per server)
- Shared search-and-execute action framework (`search_actions` + `execute_action`) with schema-validated dispatch and a structured `ActionOutcome`
- Standardized response envelope and canonical error/telemetry model shared across all servers
- Vectorized knowledge base (LanceDB + local `bge-base-en-v1.5` embeddings) for offline semantic search over NXP documentation - no cloud calls, no data leaving the machine
- Standalone mode - the knowledge base server can run on its own with its own config file

## Table of Contents

- [MCP Core](#mcp-core)
  - [Features](#features)
  - [Table of Contents](#table-of-contents)
  - [Prerequisites](#prerequisites)
  - [How to Install](#how-to-install)
    - [Option A - Install from a GitHub release](#option-a---install-from-a-github-release)
    - [Option B - Install from source](#option-b---install-from-source)
    - [Option C - Self-installing package (.pyz)](#option-c---self-installing-package-pyz)
    - [Verify the installation](#verify-the-installation)
  - [Repository Layout](#repository-layout)
  - [Scripts](#scripts)
  - [Registering the MCP servers into AI Agents](#registering-the-mcp-servers-into-ai-agents)
    - [VS Code (CLI)](#vs-code-cli)
    - [Manual registration](#manual-registration)
  - [Configuration](#configuration)
    - [Shared Configuration Options](#shared-configuration-options)
      - [Logging](#logging)
    - [Running the Gateway](#running-the-gateway)
      - [Gateway config structure](#gateway-config-structure)
      - [Gateway config example](#gateway-config-example)
    - [Running a Standalone Server](#running-a-standalone-server)
      - [Example: Knowledge Base standalone](#example-knowledge-base-standalone)
  - [Knowledge Base](#knowledge-base)
  - [Building from Source](#building-from-source)
  - [Troubleshooting](#troubleshooting)
  - [License](#license)
  - [Community](#community)

---

## Prerequisites

| Prerequisite | Version | Link |
|--------------|---------| ---- |
| Python | 3.11 - 3.14 | [Download](https://www.python.org/downloads/) |
| AI Agent (MCP compatible) | latest | Any MCP-compatible agent that can launch a standalone MCP server - such as [Claude Desktop](https://claude.com/download), [VS Code](https://code.visualstudio.com/) or [Cursor](https://www.cursor.com/), among others |

> **Note** MCP Core provides the gateway, the knowledge base, and the shared infrastructure only. To drive a specific NXP tool (S32 Design Studio, S32 Flash Tool, FreeMASTER Lite, ...), install that tool's MCP server from its own repository; it registers under the `nxp.mcp.servers` entry-point group and the gateway will discover it automatically.

---

## How to Install

### Option A - Install from a GitHub release

Use the bundled updater to compare the installed NXP MCP packages against a GitHub release and download/install the release wheels. Every release carries a JSON manifest (`release-manifest.json`) that declares what it ships and what each package requires, so the updater can tell whether a release is compatible with your environment before downloading anything. `--repo` is required on every invocation.

```shell
# Check what is installed vs. the latest release of this repo
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core check

# Download and install (interactive)
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core update

# Non-interactive
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core update --yes
```

See [`scripts/README.md`](scripts/README.md#github_update_toolkitpy---update-from-a-github-release) for all commands and options.


### Option B - Install from source

```shell
# 1. Create and activate a virtual environment (Python 3.11 - 3.14)
python -m venv .venv
.venv\Scripts\activate.bat      # Windows (use source .venv/bin/activate on Linux/macOS)

# 2. Install the Core packages (editable) plus dev/telemetry dependencies
pip install -r requirements.txt
```

### Option C - Self-installing package (.pyz)

Build a single-file installer from the sources and run it. It creates its own virtual environment in the destination folder, installs the Core packages, the configs, the `run-mcp` launchers and the updater, and can register the gateway with your AI agents.

```shell
# 1. Build the wheels (needs: pip install build setuptools wheel)
python scripts/build_modules.py --clean

# 2. Bundle them into the installer
python scripts/create_deploy_package.py --output dist/mcp-core-installer-2.0.0.pyz

# 3. Run the installer (interactive; see the guide for all options)
python dist/mcp-core-installer-2.0.0.pyz
```

See [`scripts/installer/README.md`](scripts/installer/README.md) for the installer options, the installation layout and the agent registration, and [`scripts/README.md`](scripts/README.md#create_deploy_packagepy---build-the-self-installing-package) for the build options.

### Verify the installation

Start the gateway once and confirm it comes up without errors:

```shell
run-mcp.bat nxp.mcp.gateway configs/gateway.stdio.yaml
```

Then restart your AI agent and ask it to list the available tools - the NXP tools appear with an `nxp_` prefix. If something is missing, see [Troubleshooting](#troubleshooting).

---

## Repository Layout

| Path | Description |
|---|---|
| `packages/` | Core Python sub-packages: `mcp_gateway` (aggregator), `mcp_knowledge` (knowledge base), and `mcp_shared` (common infrastructure) |
| `configs/` | Ready-to-use YAML configs - `gateway.stdio.yaml`, `gateway.http.yaml`, and `knowledge.standalone.yaml` |
| `scripts/` | Build, packaging and update utilities (`build_modules.py`, `create_deploy_package.py`, `github_update_toolkit.py`) and their docs |
| `scripts/installer/` | Self-installing package sources (installer entry point, manifest, `register_agents.py`) and the installation guide |
| `run-mcp.bat` / `run-mcp.sh` | Cross-platform launcher used by agent registrations |
| `SBOM-MCP-Core.spdx` | SPDX software bill of materials for the release |
| `LICENSE`, `LICENSE_ADDENDUM` | NXP Online Code Hosting Software License and the AI Addendum |
| `RELEASE_NOTES.md` | Version history and known limitations |

## Scripts

Utility scripts live in the `scripts/` directory. See [`scripts/README.md`](scripts/README.md) for full details.

| Script | Description |
|---|---|
| `scripts/build_modules.py` | Build `.whl` wheels for all sub-packages and the root meta-package |
| `scripts/create_deploy_package.py` | Bundle the wheels, configs and helper scripts into a self-installing `.pyz` (see [Option C](#option-c---self-installing-package-pyz)) |
| `scripts/installer/register_agents.py` | Register the gateway with Claude Desktop, Vero, VS Code, Cursor or Goose (installed next to `run-mcp.bat` by the `.pyz`) |
| `scripts/github_update_toolkit.py` | Compare installed NXP MCP packages against a GitHub release and download/install release assets (repo-split updater) |

---

## Registering the MCP servers into AI Agents

The gateway server (or the standalone knowledge base server) can be registered in any MCP-compatible AI agent so it is launched automatically.

- [Claude Desktop](https://claude.com/download)
- [VS Code](https://code.visualstudio.com/)
- [Cursor](https://cursor.com/download)

### VS Code (CLI)

```shell
code --add-mcp "{\"name\":\"nxp-mcp-gateway\",\"command\":\"C:/NXP/mcp-core/run-mcp.bat\",\"args\":[\"nxp.mcp.gateway\",\"configs/gateway.stdio.yaml\"]}"
```

### Manual registration

For manual registration with an AI agent, add an MCP server entry pointing to the gateway command.

```json
{
  "mcpServers": {
    "nxp-mcp-gateway": {
      "command": "C:/NXP/mcp-core/run-mcp.bat",
      "args": ["nxp.mcp.gateway", "configs/gateway.stdio.yaml"],
      "disabled": false
    }
  }
}
```

> **Note:** The launch command points at `run-mcp.bat` / `run-mcp.sh` + `configs/gateway.stdio.yaml` in your install directory. Some agents may need to be restarted after registration.

## Configuration

This section walks you through configuring the MCP servers via their respective configuration files.

### Shared Configuration Options

These options are available in both standalone and gateway configs.

#### Logging

```yaml
logging:
  level: INFO                         # DEBUG | INFO | WARNING | ERROR
  timestamp_format: "%Y-%m-%d %H:%M:%S"
  format: "%(asctime)s [%(name)s] %(levelname)s %(message)s"
  file: ../.tmp/logs/myserver.log     # optional - omit to log to stderr only
```

| Field | Default | Description |
|-------|---------|-------------|
| `level` | `INFO` | Log verbosity |
| `timestamp_format` | `%Y-%m-%d %H:%M:%S` | Date/time format in log lines |
| `format` | `%(asctime)s [%(name)s] %(levelname)s %(message)s` | Full Python logging format string |
| `file` | _(none)_ | Path to log file; omit to write to stderr only |

> **Note:** Relative paths in a config file (`logging.file`, `skills`, `db_path`, `corpora[].dirs`, `cache`) are resolved against the directory of that config file, not against the current working directory. The shipped configs live in `configs/`, so they use `../` to reach the installation root.

> **Note:** In the gateway, `logging` settings are inherited from the top-level `logging` block unless a per-server `logging` override is provided.

---
### Running the Gateway

The gateway loads every installed server and exposes them all through one MCP connection. Each tool is prefixed with the server name (e.g. `nxp_knowledge_kb_search`) to avoid naming conflicts.

**Start the gateway (stdio transport - default):**

The agent launches the gateway as a subprocess. Use this for most agents (Claude Desktop, VS Code with stdio).

```shell
# Run from the installation directory
python -m nxp.mcp.gateway configs/gateway.stdio.yaml
```
or
```shell
run-mcp.bat nxp.mcp.gateway configs/gateway.stdio.yaml
```

**Start the gateway (HTTP transport - shared instance):**

The gateway runs as a persistent shared process. Start it once; agents can connect to it via `http://127.0.0.1:8080/mcp`.
```shell
# Run from the installation directory
python -m nxp.mcp.gateway configs/gateway.http.yaml
```
or
```shell
run-mcp.bat nxp.mcp.gateway configs/gateway.http.yaml
```

> **Note:** The HTTP endpoint host and port are configurable via `http.host` and `http.port` in `configs/gateway.http.yaml` (default: `http://127.0.0.1:8080/mcp`).

#### Gateway config structure

A gateway config adds two sections on top of the shared `logging` block:

- `logging` - shared logging settings (inherited by all sub-servers unless overridden)
- `gateway` - gateway-level behaviour flags
- `servers` - one entry per server, with its settings and optional overrides

#### Gateway config example

```yaml
logging:
  level: INFO
  file: ../.tmp/logs/gateway.log      # optional - omit for stderr only

gateway:
  strict_startup: true                # fail to start if any enabled server fails to load
  skills: []                          # optional: list of extra skill directories to expose

servers:
  knowledge:
    settings:
      db_path: "../knowledge_db/bge-base-en-v1.5"
      corpora:
        - name: "s32ds"
          dirs:
            - "../knowledge/s32ds"
      chunk_size: 1200
      chunk_overlap: 200
      embedder:
        provider: "sentence_transformers"
        model: "BAAI/bge-base-en-v1.5"
        dimension: 768
        device: "cpu"
        cache: "../models"
        offline: true
      mutable: false
```

> **How opt-out works:** Servers that are installed but not listed here are still discovered automatically. To explicitly prevent a server from loading, add `disabled: true` to its entry.

---
### Running a Standalone Server

A standalone config (specific to a single server) has three sections:

- `logging` - logging settings
- `settings` - server-specific settings
- `skills` - path to the skills directory for this server (optional)

#### Example: Knowledge Base standalone

Use this when you want semantic search over NXP documentation.

**Start the server:**
```shell
# Run from the installation directory
python -m nxp.mcp.knowledge configs/knowledge.standalone.yaml
```
or
```shell
run-mcp.bat nxp.mcp.knowledge configs/knowledge.standalone.yaml
```

**Config file (`configs/knowledge.standalone.yaml`):**
```yaml
logging:
  level: INFO
  file: ../.tmp/logs/knowledge.log              # optional - omit for stderr only

settings:
  db_path: "../knowledge_db/bge-base-en-v1.5"   # directory where LanceDB stores vector data
                                                # (one folder, one table per corpus)
  corpora:                                      # one named corpus per LanceDB table
    - name: "s32ds"                             # corpus name used as the LanceDB table name
      dirs:                                     # directories scanned for this corpus at startup
        - "../knowledge/s32ds"
  chunk_size: 1200                              # max words per document chunk
  chunk_overlap: 200                            # word overlap between consecutive chunks
  embedder:
    provider: "sentence_transformers"
    model: "BAAI/bge-base-en-v1.5"              # path to a pre-downloaded model
    dimension: 768                              # must match the model (768 for bge-base-en-v1.5)
    device: "cpu"                               # "cpu", "cuda", or "mps"
    cache: "../models"                          # directory (config-relative) where models are cached / downloaded
    offline: true                               # true = load only from local cache (no HuggingFace Hub calls); false = allow download
  mutable: false                                # true = allow adding/removing documents via tools
```
---

## Knowledge Base

The `mcp_knowledge` server provides a fully local, vectorized documentation index:

- **Storage** - LanceDB tables under `knowledge_db/bge-base-en-v1.5/`, one table per corpus.
- **Embeddings** - `bge-base-en-v1.5` (768 dimensions), executed locally on CPU (or CUDA / MPS if configured).
- **Reranking** - an optional cross-encoder (`bge-reranker-base`) can be enabled for higher-precision result ordering.
- **Corpora** - one per tool domain; corpora can be grouped so a single query fans out across multiple tables.
- **Tools** - `nxp_knowledge_kb_search` (semantic search) and `nxp_knowledge_kb_list_corpora` (discover groups and tables). With `mutable: true`, `kb_ingest` / `kb_remove` become available for adding your own documents.

Everything runs offline - no documentation, source code or query text is sent to any external service by the knowledge base.

> **Note:** The knowledge base ships without a prebuilt index or embedding model. Provide a prebuilt LanceDB index (and a cached model under the configured `cache` directory), or run an ingestion with `mutable: true`, before semantic search returns results.

## Building from Source

The repository is a multi-package Python workspace. Each package under `packages/mcp_<name>/` owns its own `pyproject.toml` and can be built and installed independently. `mcp_gateway` and `mcp_knowledge` declare an `nxp.mcp.servers` entry-point; `mcp_shared` is the common library they depend on. The root `pyproject.toml` is a convenience meta-package that pulls the Core packages in.

```shell
# 1. Create and activate a virtual environment (Python 3.11 - 3.14)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. Install dependencies (editable Core packages + dev/telemetry)
pip install -r requirements.txt

# 3. Build wheels for all sub-packages plus the root meta-package
#    (use --no-root for the wheels attached to the GitHub release)
python scripts/build_modules.py
```

The built wheels are written to the `dist/` directory. See [`scripts/README.md`](scripts/README.md) for the full script reference.

`pip install -r requirements.txt` already installs the Core packages in editable mode, so a server can be started directly from the source checkout by pointing the module at a config file:

```shell
python -m nxp.mcp.gateway configs/gateway.stdio.yaml
```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Agent shows no `nxp_*` tools | The MCP server entry was not picked up by the agent. Re-check the registration (command + config path), then restart the agent. |
| Gateway exits immediately at startup | With `gateway.strict_startup: true`, one failing server aborts the whole gateway. Check the log file configured under `logging.file`, then either fix the server's `settings` or mark it `disabled: true`. |
| An installed tool server does not appear | The server is not installed in the same Python environment as the gateway, or its `nxp.mcp.servers` entry-point is not registered. Reinstall it into the gateway's environment. |
| Knowledge search returns nothing | The corpus is empty or `db_path` points to the wrong folder. Confirm the LanceDB index exists and that the corpus name matches the `kb_list_corpora` output. |
| `pip` cannot find `nxp-mcp-shared` when updating a tool server | Releases are published to GitHub only (no package index). Re-run the update with `--with-deps` so the updater also fetches the NXP dependencies declared in the release's `release-manifest.json` (see [`scripts/README.md`](scripts/README.md#github_update_toolkitpy---update-from-a-github-release)). |
| The updater reports that a release is not updater-compatible | The release has no `release-manifest.json` asset attached. Pick another release with `--tag`, or install from source (Option B). |


Enable `logging.level: DEBUG` and set `logging.file` in the relevant config to capture a full trace before reporting an issue.

## License

This software is licensed under the **LA_OPT_Online Code Hosting NXP_Software_License - v1.4 May 2025**, together with an **AI Addendum** that governs the agentic / AI-assisted functionality of this toolkit.

- Full license text: [`LICENSE`](LICENSE)
- AI Addendum: [`LICENSE_ADDENDUM`](LICENSE_ADDENDUM)
- Third-party components and their licenses: [`SBOM-MCP-Core.spdx`](SBOM-MCP-Core.spdx)

By downloading, installing or using this software you accept the terms of the license agreement and the addendum. If a separate license agreement for this software has been signed by you and NXP, that agreement governs your use and supersedes the terms above.

## Community

Questions, feedback, feature requests and discussions about this toolkit are welcome on the NXP Community:

[Agentic AI Development - NXP Community](https://community.nxp.com/t5/Agentic-AI-Development/tkb-p/agentic-ai-development)

When reporting a problem, please include the version (see [`RELEASE_NOTES.md`](RELEASE_NOTES.md)), your operating system and Python version, the agent you are using, and the relevant portion of the server log captured with `logging.level: DEBUG`.
