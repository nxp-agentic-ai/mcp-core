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

"""Detection registry and scan engine.

The engine walks declared search roots and reports what exists. It is read-only
and never raises on an unreadable path: an undetectable component is reported as
absent, which is a valid answer, not a failure.

Detection is optional for a product package. A component with no registered
spec simply resolves to ``None``, so a server that contributes nothing here
keeps working.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Sequence

from .models import DetectedTool, DetectorSpec

logger = logging.getLogger(__name__)


class DetectorRegistry:
    """Component name -> :class:`DetectorSpec`.

    Registration is explicit at import time, mirroring ``INSTALL_PROVIDERS``, so
    the set of detectors is deterministic and greppable.
    """

    def __init__(self) -> None:
        self._specs: dict[str, DetectorSpec] = {}

    def register(self, spec: DetectorSpec, *, replace: bool = False) -> None:
        if not spec.component:
            raise ValueError("DetectorSpec.component must be set")
        if spec.component in self._specs and not replace:
            raise ValueError(f"A detector for '{spec.component}' is already registered")
        self._specs[spec.component] = spec

    def get(self, component: str) -> DetectorSpec | None:
        return self._specs.get(component)

    def components(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[DetectorSpec, ...]:
        return tuple(self._specs[name] for name in self.components())

    # -- detection ---------------------------------------------------------

    def find_all(self, component: str) -> tuple[DetectedTool, ...]:
        """Every installation of ``component``, newest first.

        An unregistered component yields nothing rather than raising, so a
        caller can ask about anything.
        """

        spec = self._specs.get(component)
        if spec is None:
            return ()
        return tuple(_scan(spec, self))

    def find(self, component: str) -> DetectedTool | None:
        """The preferred installation of ``component``, or ``None``."""

        found = self.find_all(component)
        return found[0] if found else None

    def snapshot(self, components: Iterable[str] | None = None) -> dict[str, str | None]:
        """``{component: install_root or None}``, as the install layer expects.

        Omitting ``components`` detects everything registered.
        """

        names = tuple(components) if components is not None else self.components()
        result: dict[str, str | None] = {}
        for name in names:
            found = self.find(name)
            result[name] = found.root if found else None
        return result

    def report(self, components: Iterable[str] | None = None) -> dict[str, object]:
        """Detection detail for an agent: every install found, per component."""

        names = tuple(components) if components is not None else self.components()
        detail: dict[str, object] = {}
        for name in names:
            spec = self._specs.get(name)
            found = self.find_all(name)
            detail[name] = {
                "detected": bool(found),
                "known": spec is not None,
                "displayName": spec.label() if spec else name,
                "installations": [tool.to_dict() for tool in found],
            }
        return detail


DETECTORS = DetectorRegistry()


def register_detector(spec: DetectorSpec, *, replace: bool = False) -> None:
    DETECTORS.register(spec, replace=replace)


def detect(components: Iterable[str] | None = None) -> dict[str, str | None]:
    return DETECTORS.snapshot(components)


def detect_one(component: str) -> DetectedTool | None:
    return DETECTORS.find(component)


def detect_all(component: str) -> tuple[DetectedTool, ...]:
    return DETECTORS.find_all(component)


# -- internals -------------------------------------------------------------


def _scan(spec: DetectorSpec, registry: DetectorRegistry) -> list[DetectedTool]:
    pattern = re.compile(spec.dir_pattern, re.IGNORECASE) if spec.dir_pattern else None

    found: list[tuple[tuple[int, ...], DetectedTool]] = []
    for base in _search_bases(spec, registry):
        for candidate in _candidate_dirs(base, pattern):
            version_key, version_label = (
                spec.version_from(candidate.name) if spec.version_from else ((), "")
            )
            executable = _find_executable(candidate, spec)
            if spec.executables and executable is None:
                # The folder is there but the application is not, so this is not
                # a usable installation.
                logger.debug(
                    "%s: %s matches but none of %s is present",
                    spec.component,
                    candidate,
                    ", ".join(spec.executables),
                )
                continue
            found.append(
                (
                    version_key,
                    DetectedTool(
                        component=spec.component,
                        root=str(candidate),
                        display_name=spec.label(),
                        version=version_label,
                        executable=str(executable) if executable else "",
                    ),
                )
            )

    # Newest first; ties fall back to the path so the order is stable.
    found.sort(key=lambda item: (item[0], item[1].root), reverse=True)
    deduped: list[DetectedTool] = []
    seen: set[str] = set()
    for _, tool in found:
        key = os.path.normcase(tool.root)
        if key not in seen:
            seen.add(key)
            deduped.append(tool)
    return deduped


def _search_bases(spec: DetectorSpec, registry: DetectorRegistry) -> list[Path]:
    if spec.under:
        parent = registry.find(spec.under)
        if parent is None:
            return []
        bases: Sequence[Path] = (Path(parent.root),)
    elif spec.roots is not None:
        bases = spec.roots()
    else:
        return []

    expanded: list[Path] = []
    for base in bases:
        for sub in spec.search_subdirs or ("",):
            expanded.append(Path(base) / sub if sub else Path(base))
    return expanded


def _candidate_dirs(base: Path, pattern: re.Pattern[str] | None) -> list[Path]:
    """Installation directories inside ``base``.

    With no pattern, ``base`` itself is the installation. Unreadable or missing
    bases yield nothing.
    """

    try:
        if not base.is_dir():
            return []
        if pattern is None:
            return [base]
        return [
            entry
            for entry in sorted(base.iterdir())
            if pattern.match(entry.name) and entry.is_dir()
        ]
    except OSError as exc:
        logger.debug("Could not scan %s: %s", base, exc)
        return []


def _find_executable(root: Path, spec: DetectorSpec) -> Path | None:
    """First of ``spec.executables`` present under ``root``.

    Both the Windows and the Linux names are listed in one spec, so whichever
    exists wins and the spec needs no platform branching.
    """

    for sub in spec.exe_subdirs or ("",):
        folder = root / sub if sub else root
        for name in spec.executables:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None
