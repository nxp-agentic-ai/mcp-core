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

"""Shared filesystem scan helpers for locating NXP tool installations.

Single source of truth for the default NXP install roots so every MCP server
(S32CT, S32Trace, S32DS, ...) discovers the same installs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Iterator, Optional

__all__ = [
    "default_scan_roots",
    "iter_install_dirs",
    "parse_s32ds_version",
]


def default_scan_roots() -> list[Path]:
    """Default NXP install roots, in priority order.

    Non-existent entries are returned as-is; callers are expected to skip them.
    """
    roots: list[Path] = [Path(r"C:\NXP")]
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env_var)
        if val:
            roots.append(Path(val) / "NXP")
    roots.append(Path("/opt/nxp"))
    roots.append(Path("/usr/local/NXP"))
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        roots.append(Path(home) / "NXP")
    return roots


def iter_install_dirs(roots: Optional[Iterable[Path]] = None) -> Iterator[Path]:
    """Yield existing first-level subdirectories under each scan root.

    The scan is shallow and read-only. Unreadable or missing roots are skipped.
    """
    if roots is None:
        roots = default_scan_roots()
    for root in roots:
        try:
            if not Path(root).is_dir():
                continue
            entries = sorted(Path(root).iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    yield entry
            except OSError:
                continue


_S32DS_VERSION_RE = re.compile(
    r"^S32DS\.(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:_.*)?$",
    re.IGNORECASE,
)


def parse_s32ds_version(dirname: str) -> tuple[tuple[int, ...], str]:
    """Return (version_tuple, label) for a folder name like 'S32DS.3.6.9'.

    Returns ``((), "")`` when the name does not match the S32DS convention.
    """
    m = _S32DS_VERSION_RE.match(dirname)
    if not m:
        return (), ""
    major = int(m["major"])
    minor = int(m["minor"])
    patch = int(m["patch"] or 0)
    label = f"{major}.{minor}" + (f".{patch}" if patch else "")
    return (major, minor, patch), label
