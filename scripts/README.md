# Scripts

Utility scripts for building, packaging and updating MCP Core.

| Script | Description |
|---|---|
| [`build_modules.py`](#build_modulespy---build-the-wheels) | Build `.whl` wheels (and optionally sdists) for the Core sub-packages and the root meta-package |
| [`create_deploy_package.py`](#create_deploy_packagepy---build-the-self-installing-package) | Bundle the wheels, configs, launchers and helper scripts into one self-installing `.pyz` |
| [`installer/`](installer/README.md) | Installer entry point and manifest bundled into the `.pyz`, plus `register_agents.py` (agent registration). See [`installer/README.md`](installer/README.md) for the installation guide |
| [`github_update_toolkit.py`](#github_update_toolkitpy---update-from-a-github-release) | Compare the installed NXP MCP packages against a GitHub release and download / install / roll back release assets |

---

## `build_modules.py` - Build the wheels

Builds every package under `packages/` (`mcp_shared`, `mcp_gateway`, `mcp_knowledge`) and the root meta-package `nxp-mcp` as Python wheels.

### Prerequisites

| Package | Purpose |
|---|---|
| `build` | `build_modules.py` calls `python -m build` for every package |
| `setuptools` (>= 81) | Build backend declared in every `pyproject.toml` |
| `wheel` | Required by the `setuptools` backend to emit `.whl` archives |

`build` is installed by `pip install -r requirements.txt`; otherwise:

```shell
pip install build setuptools wheel
```

### Usage

```shell
python scripts/build_modules.py [--package NAME] [--no-root] [--sdist] [--outdir DIR] [--clean] [--list]
```

| Option | Description |
|---|---|
| `--package`, `-p NAME` | Build only this package (directory name under `packages/`, e.g. `mcp_shared`) |
| `--no-root` | Skip the root meta-package `nxp-mcp` |
| `--sdist` | Also produce source distributions (`.tar.gz`) |
| `--outdir`, `-o DIR` | Output directory for the built artifacts (default: `dist/`) |
| `--clean` | Remove the output directory before building |
| `--list` | List the discovered packages and exit without building |

### Examples

```shell
# Build all sub-packages + the root meta-package into dist/
python scripts/build_modules.py

# Clean build of the sub-packages only
python scripts/build_modules.py --clean --no-root

# Build one package
python scripts/build_modules.py --package mcp_knowledge

# See what would be built
python scripts/build_modules.py --list
```

---

## `create_deploy_package.py` - Build the self-installing package

Collects the wheels and the assets listed in [`installer/_manifest.py`](installer/_manifest.py) and writes them, together with the installer entry point [`installer/__main__.py`](installer/__main__.py), into a single runnable `.pyz` archive.

The MCP Core package contains:

| Component | Content |
|---|---|
| `common` (always installed) | `nxp-mcp-shared` wheel, `LICENSE`, `LICENSE_ADDENDUM`, `SBOM.spdx.json`, `README.md` (from `installer/README.md`), `run-mcp.bat`, `run-mcp.sh`, `register_agents.py`, `scripts/github_update_toolkit.py` |
| `gateway` | `nxp-mcp-gateway` wheel, `configs/gateway.stdio.yaml`, `configs/gateway.http.yaml` |
| `knowledge` | `nxp-mcp-knowledge` wheel, `configs/knowledge.standalone.yaml` |

No skills, embedding model or knowledge index are bundled. Missing asset files are reported as `WARNING` and skipped; a missing wheel stops the build.

### Prerequisites

- Python 3.11+ (the script itself needs only the standard library)
- The wheels in `dist/`, built with `build_modules.py`

### Usage

```shell
python scripts/create_deploy_package.py [--output PATH] [--version X.Y.Z] [--whl-dir DIR] [--fetch-external]
```

| Option | Description |
|---|---|
| `--output PATH` | Output `.pyz` path (default: `dist/mcp-core-installer-<version>.pyz`) |
| `--version X.Y.Z` | Version stamped into the installer (default: `[project].version` from the root `pyproject.toml`) |
| `--whl-dir DIR` | Additional folder with pre-built wheels, searched after `dist/` (repeatable) |
| `--fetch-external` | Download wheels declared under `[[tool.nxp-mcp.external]]` in the root `pyproject.toml`. MCP Core declares none, so this is a no-op here |

Every required package must resolve to exactly one wheel; the build stops when a wheel is missing, ambiguous, or does not satisfy the version another bundled wheel requires. Use only the default (`standard`) variant: this repository has no extended installer manifest.

### Examples

```shell
# 1. Build the wheels
python scripts/build_modules.py --clean

# 2. Bundle them into the self-installing package
python scripts/create_deploy_package.py --output dist/mcp-core-installer-2.0.0.pyz

# 3. Install (see installer/README.md for all installer options)
python dist/mcp-core-installer-2.0.0.pyz
```

---

## `github_update_toolkit.py` - Update from a GitHub release

Compares the NXP MCP packages installed in the current Python environment against a GitHub release and reports, per package, what is up to date, outdated or missing. It can also list releases, download release assets, install an update and roll back content.

Every release attaches a small JSON manifest, `release-manifest.json`, that declares what the release ships, what each package requires (NXP and third-party), where the NXP dependencies come from, and which content archives (configs / skills / kb) are attached. The manifest is mandatory: a release without it is reported as not updater-compatible.

The script uses only the Python standard library.

### Usage

```shell
python scripts/github_update_toolkit.py --repo OWNER/NAME [--token TOKEN] [--tag vX.Y.Z] {check,list,download,update,rollback} ...
```

| Global option | Description |
|---|---|
| `--repo OWNER/NAME` | **Required.** GitHub repository as `OWNER/NAME` or a full GitHub URL (for this repo: `nxp-agentic-ai/mcp-core`) |
| `--token TOKEN` | GitHub token for private repositories or higher rate limits (default: `GITHUB_TOKEN` / `GH_TOKEN` environment variable) |
| `--tag vX.Y.Z` | Target a specific release tag instead of the latest release |

| Command | Description |
|---|---|
| `check` | (default) Report installed vs. release, per package, and whether the release is compatible with this environment. No download |
| `list` | List published releases (or raw git tags with `--tags`) with a compatibility column |
| `download` | Download release assets - wheels (default), the manifest, or a content archive. No install |
| `update` | `check` -> plan -> download -> install the wheels with `pip`, then apply the content archives |
| `rollback` | Restore content from the backup of a previous update |

Run `python scripts/github_update_toolkit.py --repo OWNER/NAME <command> --help` for the options of each command.

### Examples

```shell
# Check the installed packages against the latest MCP Core release
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core check

# Check against a specific release
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core --tag v2.0.0 check

# List published releases (or raw git tags)
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core list
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core list --tags --limit 20

# Download the release wheels, or only the release manifest
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core download
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core download --kind manifest

# Update (interactive)
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core update

# Update a tool server and also pull the NXP dependencies (e.g. nxp-mcp-shared) that block the install
python scripts/github_update_toolkit.py --repo nxp-agentic-ai/freemaster-ai-support update --with-deps

# Only update one kind of content, or skip some kinds
python scripts/github_update_toolkit.py --repo OWNER/NAME update --only skills
python scripts/github_update_toolkit.py --repo OWNER/NAME update --skip kb
```

> **Note:** `update` installs the wheels into the Python environment that runs the script. Run it with the interpreter of the target installation, for example `C:/NXP/MCP/.venv/Scripts/python.exe scripts/github_update_toolkit.py --repo nxp-agentic-ai/mcp-core update --yes` for a non-interactive update of that virtual environment.
