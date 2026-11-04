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

"""Data models for filesystem-based tool detection.

Detection answers one question only: is this product present on disk, and
where. It is deliberately not coupled to how the product was installed - in
particular it never inspects an Eclipse p2 profile or queries an update site.
Which installable units a Design Studio carries is a question for the install
layer, not for detection.

A detector is therefore a declaration of paths and executable names, not code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# The host every Eclipse p2 update-site install goes into. The shared install
# layer looks it up by name; its detector is contributed by the s32ds package.
S32DS_COMPONENT = "s32ds"

# Maps a directory name to ``(sortable_version, label)``. ``((), "")`` means the
# name carries no version, which is not an error - the install is still valid.
VersionParser = Callable[[str], tuple[tuple[int, ...], str]]

# Where to start looking. A callable is evaluated per detection run so that
# environment changes are picked up without re-importing anything.
RootsProvider = Callable[[], Sequence[Path]]


@dataclass(frozen=True, slots=True)
class DetectedTool:
    """One installation found on disk."""

    component: str
    root: str
    display_name: str = ""
    version: str = ""
    # The executable that proved the install, when the detector requires one.
    # Empty for directory-backed components such as an RTD payload, where the
    # install root is the whole answer.
    executable: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "component": self.component,
            "root": self.root,
            "displayName": self.display_name,
        }
        if self.version:
            payload["version"] = self.version
        if self.executable:
            payload["executable"] = self.executable
        return payload


@dataclass(frozen=True, slots=True)
class DetectorSpec:
    """How to find one component on disk.

    Two shapes are expressible, and both are plain filesystem checks:

    * **executable-backed** - an application. A directory matching
      ``dir_pattern`` must exist *and* contain one of ``executables``. Used for
      S32 Design Studio, FlashTool, FreeMASTER and similar.
    * **directory-backed** - ``executables`` is empty, so the presence of the
      directory is itself the detection and what is reported is the root the
      component is installed in. Used for payload-style components such as an
      RTD or a configuration-tools data package.

    ``under`` expresses a component installed inside another one (an RTD lives
    under a Design Studio root). It is still only a directory check: the parent
    is detected first and its root becomes the search base.
    """

    component: str
    display_name: str = ""

    # Search bases. Ignored when ``under`` is set.
    roots: RootsProvider | None = None
    # Component whose detected root is the search base, e.g. "s32ds".
    under: str = ""
    # Subdirectories of each base to look in. "" means the base itself.
    search_subdirs: tuple[str, ...] = ("",)

    # Regex matched against a directory name inside a search base, e.g.
    # r"^S32DS\.\d+\.\d+(\.\d+)?$" for the variable-version S32DS folder. Empty
    # means the search base is itself the installation.
    dir_pattern: str = ""
    version_from: VersionParser | None = None

    # Any one of these existing proves the install. Empty = directory-backed.
    executables: tuple[str, ...] = ()
    # Where under the install root the executables live. "" means the root.
    exe_subdirs: tuple[str, ...] = ("",)

    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def label(self) -> str:
        return self.display_name or self.component

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "displayName": self.label(),
            "under": self.under,
            "dirPattern": self.dir_pattern,
            "executables": list(self.executables),
            "notes": self.notes,
        }
