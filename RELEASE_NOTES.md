## Overview

MCP Core is the shared foundation of the NXP MCP server suite - the infrastructure every NXP MCP tool server is built on:

- Root gateway that dynamically discovers and mounts installed MCP servers behind a single MCP connection
- Shared search-and-execute action framework with a standardized response/error model
- LanceDB-backed knowledge base for offline semantic search over NXP tool documentation

---

## v2.0.0 (05.11.2026)

### Changed

- MCP Core (`nxp-mcp-shared`, `nxp-mcp-gateway`, `nxp-mcp-knowledge`) is now released from its own repository, [nxp-agentic-ai/mcp-core](https://github.com/nxp-agentic-ai/mcp-core). In v1.0.0 these packages were released as part of S32 Design Studio AI Support. Tool servers are released from their own repositories and depend on the `nxp-mcp-shared` wheel attached to each mcp-core release.
- FastMCP updated to 4.1.0.
- Dependencies use version ranges instead of exact pins. Supported Python versions: 3.11 - 3.14.
- The self-installing package (`mcp-core-installer-<version>.pyz`) now installs only the Core packages, configs, `run-mcp` launchers and the updater.
- Knowledge base: falls back to CPU when CUDA is configured but not available.

### Added

- Shared tool detection and provisioning framework (`nxp.mcp.shared.detect`, `nxp.mcp.shared.install`): tool servers register detectors and install providers to locate, report and install the NXP products they drive.
- OpenTelemetry tracing: the shared action framework emits a span per tool call; `run-mcp --telemetry` exports them over OTLP.
- Manifest-driven GitHub release updater (`scripts/github_update_toolkit.py`): every release carries a `release-manifest.json`; `--repo` selects the repository and `update --with-deps` also fetches the NXP dependencies a release needs.

---

## v1.0.0 (28.09.2026)

Initial release of the Core packages, as part of S32 Design Studio AI Support v1.0.0.

### Key Features

- Gateway composition with dynamic server discovery via the `nxp.mcp.servers` entry-points, `nxp_<name>` namespacing and `disabled: true` opt-out
- Shared search-and-execute action framework: `search_actions` (BM25 and regex strategies) and `execute_action` with schema-validated dispatch returning a structured `ActionOutcome`
- Standardized response envelope and canonical error-code registry (`standardize_tool_response`, `install_envelope_middleware`)
- OpenRPC synthesis and a generic JSON-RPC client (`RpcClient`) for tool backends that advertise `rpc.discover`
- Knowledge base: LanceDB semantic search with local `bge-base-en-v1.5` embeddings, optional cross-encoder reranking and opt-in ANN (IVF_PQ) acceleration
- Self-installing `.pyz` installer with automatic agent registration

---

## Known Limitations

- Tool servers are not bundled: the gateway only exposes servers that are installed separately and registered under the `nxp.mcp.servers` entry-point group.
- Releases are published to GitHub only (no package index); cross-repo dependency wheels must be fetched alongside with `github_update_toolkit.py update --with-deps` (see `scripts/README.md`).
- The knowledge base ships without a prebuilt index; a LanceDB index must be provided or an ingestion run performed before semantic search returns results.

## Licensing

This release is distributed under the **LA_OPT_Online Code Hosting NXP_Software_License - v1.4 May 2025**, together with the **AI Addendum**.
