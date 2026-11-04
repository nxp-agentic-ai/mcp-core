# Copyright 2026 NXP
#
# NXP Proprietary. This software is owned or controlled by NXP and may
# only be used strictly in accordance with the applicable license terms.
# By expressly accepting such terms or by downloading, installing,
# activating and/or otherwise using the software, you are agreeing that
# you have read, and that you agree to comply with and are bound by,
# such license terms.  If you do not agree to be bound by the applicable
# license terms, then you may not retain, install, activate or otherwise
# use the software.

"""Shared manifest for the MCP Core installer.

This module is the single source of truth for all values shared between the
package-creation script (scripts/create_deploy_package.py) and the installer
entry point (scripts/installer/__main__.py).  It is bundled verbatim into
every .pyz archive so both sides read from the same data.

MCP Core ships three Python packages (nxp-mcp-shared, nxp-mcp-gateway,
nxp-mcp-knowledge), their configs, the agent-registration helper and the
GitHub release updater. It ships no skills, no embedding model and no
prebuilt knowledge index; those are delivered separately (for example by a
tool server release or by the updater).

Path strings in component definitions are relative to the repository root.
create_deploy_package.py resolves them against PROJECT_ROOT at build time; the
installer discovers assets by scanning the archive.

The shared utility helpers (normalize, whl_dist_name, whl_version,
required_whl_names) live here so both the build script and the installer read
from one implementation.
"""

import re
from pathlib import Path

# Installer version.
# "@@VERSION@@" is a placeholder replaced by create_deploy_package.py at
# bundle time.  The --version CLI argument (or the root pyproject.toml
# version when omitted) is substituted in-memory before the manifest is
# written into the .pyz archive.  This file is never modified on disk.
VERSION = "@@VERSION@@"

# Archive prefix where wheel files are staged inside the .pyz.
WHL_DIR = "whl"

# Archive entries that belong to the installer itself and must not be
# extracted to the installation destination.
INSTALLER_ENTRIES = frozenset(
    {"__main__.py", "_manifest.py"}
)

# Name of the reserved component holding shared assets that are always
# installed regardless of which components the user selects.
DEFAULT_COMPONENT = "common"


# --- Shared utilities --------------------------------------------------------
# Single source of truth for package-name and wheel handling.  Imported by both
# create_deploy_package.py (build time) and __main__.py (install time).

def normalize(name: str) -> str:
    """Normalise a package name: lower-case, collapse [-_.]+ to '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def whl_dist_name(filename) -> str:
    """Return the normalised distribution name from a wheel filename or path.

    Wheel filenames are: {name}-{ver}-{pyver}-{abi}-{plat}.whl
    PEP 427 mandates the name component uses '_' instead of '-'; both are
    normalised to '-' so comparisons are reliable.
    """
    return normalize(Path(str(filename)).name.split("-")[0])


def whl_version(filename) -> str:
    """Return the version component from a wheel filename or path.

    Wheel filenames are: {name}-{ver}-{pyver}-{abi}-{plat}.whl, and PEP 427
    forbids '-' inside both {name} and {ver}, so the version is always the
    second '-'-separated field. Returns an empty string when the filename
    does not follow that layout.
    """
    parts = Path(str(filename)).name.split("-")
    return parts[1] if len(parts) >= 5 else ""


def required_whl_names(components: dict) -> set:
    """Return the set of normalised wheel names referenced by all components."""
    return {
        normalize(whl)
        for comp in components.values()
        for whl in comp.get("whl", [])
    }


# A component (MCP server) definition.
# - whl:     list[str]                 - wheel distribution names, matched
#                                        against the collected *.whl files
# - files:   list[tuple[str, str]]     - (source_file,   dest_file)
# - folders: list[tuple]               - (source_folder, dest_folder) or
#                                        (source_folder, dest_folder, [excludes])
#                                        where [excludes] is a list of
#                                        source-relative subpaths to skip.
# All source paths are repo-root-relative; dest paths are archive-relative.
#
# Component names map 1:1 to server keys in configs/gateway.*.yaml; the
# installer marks unselected components as "disabled: true" there.

COMPONENTS = {
    DEFAULT_COMPONENT: {
        "whl": ["nxp-mcp-shared"],
        "files": [
            ("LICENSE", "LICENSE"),
            ("LICENSE_ADDENDUM", "LICENSE_ADDENDUM"),
            ("SBOM-MCP-Core.spdx.json", "SBOM.spdx.json"),
            ("scripts/installer/README.md", "README.md"),
            ("run-mcp.bat", "run-mcp.bat"),
            ("run-mcp.sh", "run-mcp.sh"),
            ("scripts/installer/register_agents.py", "register_agents.py"),
            # The updater derives the install root from its own location
            # (<install-root>/scripts/github_update_toolkit.py), so it must be
            # placed under scripts/ for configs/, skills/ and knowledge_db/
            # updates to land in the install root.
            ("scripts/github_update_toolkit.py", "scripts/github_update_toolkit.py"),
        ],
        "folders": [],
    },
    "gateway": {
        "whl": ["nxp-mcp-gateway"],
        "files": [
            ("configs/gateway.stdio.yaml", "configs/gateway.stdio.yaml"),
            ("configs/gateway.http.yaml", "configs/gateway.http.yaml"),
        ],
        "folders": [],
    },
    "knowledge": {
        "whl": ["nxp-mcp-knowledge"],
        "files": [
            ("configs/knowledge.standalone.yaml", "configs/knowledge.standalone.yaml"),
        ],
        "folders": [],
    },
}
