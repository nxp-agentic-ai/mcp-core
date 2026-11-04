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

"""Provider base classes for the shared install layer.

All install mechanics live here. A product package contributes a provider by
subclassing one of the bases below and declaring class constants plus
``offers()``; it never writes subprocess, property-file or command-construction
code of its own.

Three bases, matching the three supported methods:

* :class:`ExeInstallerProvider`  - product ships its own silent installer
  (InstallAnywhere ``-i silent -f <properties>``). Fresh installs and upgrades.
* :class:`P2UpdateSiteProvider`  - component is added into an already-installed
  S32 Design Studio through the Eclipse p2 director.
* :class:`GuidanceProvider`      - cannot be automated; return a download
  location and instructions.

Installs are never interactive: ``INSTALLER_UI`` is always ``silent`` and every
p2 invocation carries ``-noSplash``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping, Sequence

from ..detect import DETECTORS, S32DS_COMPONENT
from .models import (
    InstallDelivery,
    InstallMethod,
    InstallMode,
    InstallOffer,
    InstallRequest,
    InstallResult,
    InstallStatus,
    SkippedUnit,
)
from .source import (
    ArtifactKind,
    InstallSource,
    SourceLookup,
    fetch_to_temp,
    is_remote,
    resolve_installer,
    resolve_source,
)
from .sources_catalog import catalog_urls_for

logger = logging.getLogger(__name__)


# Platform tokens used in COMPONENT_MAP entries.
ALL_PLATFORMS = frozenset({"windows", "linux"})
WINDOWS_ONLY = frozenset({"windows"})
LINUX_ONLY = frozenset({"linux"})

# How much captured output to keep in the result envelope.
_OUTPUT_TAIL_CHARS = 4000

# Properties the layer renders itself; extra_properties may not override them.
_RESERVED_PROPERTY_KEYS = (
    "USER_INSTALL_DIR",
    "UPGRADE_INSTALL",
    "INSTALLER_UI",
    "TYPICAL",
    "CUSTOM",
)
_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_FORBIDDEN_VALUE_CHARS = ("\r", "\n", "\0")


def current_platform() -> str:
    return "windows" if os.name == "nt" else "linux"


def _tail(text: str | None) -> str:
    if not text:
        return ""
    return text[-_OUTPUT_TAIL_CHARS:]


class InstallProvider(ABC):
    """Contract every product-specific install provider implements."""

    PROVIDER: str = ""
    DISPLAY_NAME: str = ""
    DOWNLOAD_URL: str = ""
    NOTES: str = ""
    METHOD: InstallMethod = InstallMethod.GUIDANCE_ONLY
    DELIVERY: InstallDelivery = InstallDelivery.STANDALONE

    # Substrings identifying this product in an artifact filename. Used to route
    # "install from <path>" without the caller naming the component.
    ARTIFACT_PATTERNS: tuple[str, ...] = ()

    def __init__(self, *, source_lookup: SourceLookup | None = None) -> None:
        if not self.PROVIDER:
            raise ValueError(f"{type(self).__name__} must define PROVIDER")
        # Hook supplying candidate download locations. Results are offered as
        # suggestions, never acted on directly. Defaults to the declarative
        # catalog so a provider reports somewhere to look even when the server
        # injects no documentation lookup.
        self.source_lookup = source_lookup or catalog_urls_for


    @abstractmethod
    def offers(self) -> tuple[InstallOffer, ...]:
        """Declare which components this provider can deliver, and how."""

    @abstractmethod
    def install(self, request: InstallRequest) -> InstallResult:
        """Perform (or plan) one install and return the uniform envelope."""

    def uninstall(self, request: InstallRequest) -> InstallResult:
        return self._result(
            request,
            InstallStatus.UNSUPPORTED,
            error=f"{self.DISPLAY_NAME or self.PROVIDER} does not support uninstall.",
        )

    @classmethod
    def recognises(cls, filename: str) -> bool:
        """Whether an artifact filename looks like this product's."""

        lowered = filename.lower()
        return any(pattern.lower() in lowered for pattern in cls.ARTIFACT_PATTERNS)

    # -- helpers shared by all providers ------------------------------------

    def _result(
        self,
        request: InstallRequest,
        status: InstallStatus,
        **fields: object,
    ) -> InstallResult:
        return InstallResult(
            status=status,
            component=request.component,
            provider=self.PROVIDER,
            method=self.METHOD,
            delivery=self.DELIVERY,
            **fields,  # type: ignore[arg-type]
        )

    def _run(
        self,
        request: InstallRequest,
        command: Sequence[str],
        *,
        properties_path: str | None = None,
        properties_text: str | None = None,
        installed_path: str | None = None,
    ) -> InstallResult:
        """Execute a fully built command, honoring dry_run / confirm / wait.

        ``subprocess.run`` is used for the blocking case, which is the
        programmatic equivalent of wrapping the installer in ``cmd /k``: control
        does not return until the installer has finished.
        """

        common = {
            "command": tuple(command),
            "properties_path": properties_path,
            "properties_text": properties_text,
        }

        if request.dry_run:
            return self._result(request, InstallStatus.REQUIRES_CONFIRMATION, **common)
        if not request.confirm:
            return self._result(
                request,
                InstallStatus.REQUIRES_CONFIRMATION,
                error=(
                    "Refusing to install without explicit confirmation. Review the "
                    "command and properties, then retry with confirm=true."
                ),
                **common,
            )

        start = time.monotonic()
        try:
            if request.wait:
                completed = subprocess.run(
                    list(command),
                    capture_output=True,
                    text=True,
                    timeout=max(0, request.timeout_ms) / 1000.0,
                    check=False,
                )
                elapsed = int((time.monotonic() - start) * 1000)
                status = (
                    InstallStatus.INSTALLED
                    if completed.returncode == 0
                    else InstallStatus.FAILED
                )
                return self._result(
                    request,
                    status,
                    return_code=completed.returncode,
                    elapsed_ms=elapsed,
                    stdout=_tail(completed.stdout),
                    stderr=_tail(completed.stderr),
                    installed_path=installed_path if status is InstallStatus.INSTALLED else None,
                    error=(
                        ""
                        if status is InstallStatus.INSTALLED
                        else f"Installer exited with code {completed.returncode}."
                    ),
                    **common,
                )

            subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return self._result(
                request,
                InstallStatus.STARTED,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error="Installer started detached; completion was not awaited.",
                **common,
            )
        except subprocess.TimeoutExpired:
            return self._result(
                request,
                InstallStatus.FAILED,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=f"Installer did not finish within {request.timeout_ms} ms.",
                **common,
            )
        except OSError as exc:
            return self._result(
                request,
                InstallStatus.FAILED,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=f"Failed to launch installer: {exc}",
                **common,
            )


class ExeInstallerProvider(InstallProvider):
    """Silent InstallAnywhere installer shipped by the product itself.

    Subclasses declare:

    * ``COMPONENT_MAP`` - short component name -> (``INSTALL_*`` key, platforms).
    * ``ARTIFACT_PATTERNS`` - filename substrings identifying the product.
    * ``DEFAULT_INSTALL_DIR`` (optional) - used when the caller gives no
      ``install_dir``; when empty the installer's own default applies.

    Any file with the platform's installer extension is accepted. Product
    installer names vary between releases (``SW32_S32DS_3.6.10_RFP_D2607_...``
    vs ``S32DS.3.6.4_win32.x86_64.exe``), so matching on the product name would
    reject legitimate drops. The caller, not a filename pattern, decides what
    gets executed; ``dry_run`` surfaces the full command without running it.
    """

    METHOD = InstallMethod.EXE_INSTALLER
    DELIVERY = InstallDelivery.STANDALONE

    COMPONENT_MAP: Mapping[str, tuple[str, frozenset[str]]] = {}
    DEFAULT_INSTALL_DIR: str = ""
    # Minimum existing version an upgrade can be applied to; message only.
    UPGRADE_MIN_VERSION: str = ""

    def install(self, request: InstallRequest) -> InstallResult:
        resolved, suggestions = resolve_source(
            request.installer_path,
            component=request.component,
            provider_location=self.DOWNLOAD_URL if is_remote(self.DOWNLOAD_URL) else "",
            lookup=self.source_lookup,
            platform=current_platform(),
        )
        # Nothing to work with at all is a different problem from a location that
        # was supplied but turned out to be unusable: the first needs a location
        # from the user, the second needs the existing one corrected.
        if resolved.origin is InstallSource.UNRESOLVED:
            return self._result(
                request,
                InstallStatus.REQUIRES_SOURCE,
                suggested_sources=suggestions,
                error=(
                    f"{resolved.error} Pass installer_path pointing at the installer, a "
                    "folder containing it, or a download URL."
                    + (f" See {self.DOWNLOAD_URL}" if self.DOWNLOAD_URL else "")
                ),
            )
        if not resolved.ok:
            return self._result(
                request, InstallStatus.REQUIRES_INSTALLER_FILE, error=resolved.error
            )

        # An installer has to exist on disk before it can be run, so a remote one
        # is downloaded first.
        if resolved.kind is ArtifactKind.REMOTE_FILE:
            if not (request.confirm or request.dry_run):
                return self._result(
                    request,
                    InstallStatus.REQUIRES_CONFIRMATION,
                    guidance=f"Would download {resolved.url} and install it silently.",
                    error="Retry with confirm=true to download and install.",
                )
            if request.dry_run:
                return self._result(
                    request,
                    InstallStatus.REQUIRES_CONFIRMATION,
                    guidance=f"Would download {resolved.url} and install it silently.",
                )
            fetched = fetch_to_temp(resolved.url)
            if not fetched.ok or fetched.path is None:
                return self._result(
                    request, InstallStatus.REQUIRES_SOURCE, error=fetched.error
                )
            logger.info(
                "Downloaded %s (%d bytes in %d ms).",
                resolved.url,
                fetched.bytes_written,
                fetched.elapsed_ms,
            )
            resolved = resolve_installer(fetched.path, platform=current_platform())

        if not resolved.ok or resolved.path is None:
            return self._result(
                request, InstallStatus.REQUIRES_INSTALLER_FILE, error=resolved.error
            )

        try:
            properties_text = self.render_properties(request)
        except ValueError as exc:
            return self._result(request, InstallStatus.FAILED, error=str(exc))

        properties_path, is_temporary = self._materialize_properties(
            properties_text, request.properties_path
        )
        try:
            command = [str(resolved.path), "-i", "silent", "-f", str(properties_path)]
            return self._run(
                request,
                command,
                properties_path=str(properties_path),
                properties_text=properties_text,
                installed_path=request.install_dir or self.DEFAULT_INSTALL_DIR or None,
            )
        finally:
            if is_temporary and not request.confirm:
                # Nothing ran; do not leave a stray temp file behind.
                _unlink_quietly(properties_path)

    # -- properties rendering ----------------------------------------------

    def render_properties(self, request: InstallRequest) -> str:
        """Build the installer.properties content for one request.

        Raises ``ValueError`` when the request is internally inconsistent, for
        example component selections in typical mode, or an upgrade without a
        target directory.
        """

        mode = InstallMode(request.mode)
        install_dir = request.install_dir or self.DEFAULT_INSTALL_DIR

        if mode is InstallMode.UPGRADE and not install_dir:
            hint = (
                f" It must point at an existing {self.UPGRADE_MIN_VERSION}+ installation."
                if self.UPGRADE_MIN_VERSION
                else ""
            )
            raise ValueError(
                "Upgrade mode requires install_dir naming the installation to upgrade." + hint
            )

        lines: list[str] = []
        if install_dir:
            lines.append(f"USER_INSTALL_DIR={_escape_install_dir(install_dir)}")
        lines.append(f"UPGRADE_INSTALL={1 if mode is InstallMode.UPGRADE else 0}")
        lines.append("INSTALLER_UI=silent")

        # Typical and custom are mutually exclusive presets. An upgrade still
        # carries one of them, describing the shape of the resulting install.
        if mode is InstallMode.CUSTOM:
            lines.append("TYPICAL=0")
            lines.append("CUSTOM=1")
        else:
            lines.append("TYPICAL=1")

        selections = dict(request.components or {})
        if selections and mode is InstallMode.TYPICAL:
            raise ValueError(
                "Component selections require custom or upgrade mode; "
                "typical mode installs the predefined component set."
            )
        lines.extend(self._render_component_flags(selections))

        extras = request.extra_properties or {}
        self._validate_extra_properties(extras)
        for key, value in sorted(extras.items()):
            lines.append(f"{key}={value}")

        return "\n".join(lines) + "\n"

    def _validate_extra_properties(self, extras: Mapping[str, object]) -> None:
        reserved = {key.upper() for key in _RESERVED_PROPERTY_KEYS}
        reserved.update(key.upper() for key, _ in self.COMPONENT_MAP.values())
        for key, value in extras.items():
            key_text, value_text = str(key), str(value)
            if not _PROPERTY_KEY.match(key_text):
                raise ValueError(f"Invalid extra property key: {key_text!r}.")
            if any(ch in value_text for ch in _FORBIDDEN_VALUE_CHARS):
                raise ValueError(
                    f"Extra property {key_text} contains a line break or NUL character."
                )
            upper = key_text.upper()
            if upper in reserved or upper.startswith("INSTALL_"):
                raise ValueError(
                    f"Extra property {key_text} is reserved; use mode, install_dir "
                    "or components instead."
                )

    def _render_component_flags(self, selections: Mapping[str, bool]) -> list[str]:
        unknown = sorted(set(selections) - set(self.COMPONENT_MAP))
        if unknown:
            raise ValueError(
                f"Unknown component(s) for provider '{self.PROVIDER}': {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(self.COMPONENT_MAP))}."
            )

        platform = current_platform()
        lines: list[str] = []
        for name in sorted(selections):
            key, platforms = self.COMPONENT_MAP[name]
            if platform not in platforms:
                logger.info(
                    "Dropping component '%s' (%s): not applicable on %s.", name, key, platform
                )
                continue
            lines.append(f"{key}={1 if selections[name] else 0}")
        return lines

    @staticmethod
    def _materialize_properties(text: str, explicit_path: str | None) -> tuple[Path, bool]:
        """Write the properties file. Returns (path, is_temporary)."""

        if explicit_path:
            path = Path(explicit_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return path, False

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".properties", prefix="nxp_installer_", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(text)
        return Path(handle.name), True


class P2UpdateSiteProvider(InstallProvider):
    """Component added into an existing S32 Design Studio via the p2 director.

    Subclasses normally declare nothing but ``ARTIFACT_PATTERNS``: the update
    site comes from the caller's artifact, and the installable units are
    discovered from the site itself, so a new product drop needs no code change.

    ``REPOSITORY`` and ``IU_IDS`` exist for components served from a fixed,
    known site. Both can be overridden per request.

    Only S32 Design Studio can host p2 installs, so this provider always reports
    :data:`InstallDelivery.INTO_S32DS` and returns ``REQUIRES_EXISTING_S32DS``
    when no installation is available.
    """

    METHOD = InstallMethod.P2_UPDATE_SITE
    DELIVERY = InstallDelivery.INTO_S32DS

    REPOSITORY: str = ""
    IU_IDS: tuple[str, ...] = ()
    P2_APPLICATION = "org.eclipse.equinox.p2.director"
    _LAUNCHER_NAMES = ("s32dsc.exe", "s32dsc")
    # The director prints one "<id>=<version>" per unit, interleaved with
    # unrelated logging (SLF4J notices, a trailing timing line).
    _IU_LINE = re.compile(r"^(?P<id>[A-Za-z0-9_.\-]+)=(?P<version>[A-Za-z0-9_.\-]+)$")
    # An NXP update site marks each installable product with a unit whose id ends
    # in ".root.feature_root". That unit is a marker, not something installable:
    # the unit to pass to -installIU is its ".root.feature.feature.group"
    # sibling. For example
    #   com.nxp.RTD.S32K396.root.feature_root            <- marker
    #   com.nxp.RTD.S32K396.root.feature.feature.group   <- install this
    # Sites without such markers (e.g. S32DS development packs, which mark only
    # some products with a plain ".feature_root") are installed as every
    # ".feature.group" unit they publish.
    _ROOT_MARKER_SUFFIX = ".root.feature_root"
    _INSTALLABLE_SUFFIX = ".root.feature.feature.group"
    _FEATURE_GROUP_SUFFIX = ".feature.group"
    _DISCOVERY_TIMEOUT_S = 300.0
    # -listInstalledRoots prints "<id>/<version>".
    _INSTALLED_LINE = re.compile(r"^(?P<id>[A-Za-z0-9_.\-]+)/(?P<version>[A-Za-z0-9_.\-]+)$")
    # eclipse/features entries are "<feature id>_<version>", optionally ".jar".
    _FEATURE_DIR = re.compile(r"^(?P<id>.+?)_(?P<version>\d[A-Za-z0-9_.\-]*?)(?:\.jar)?$")
    # Director phrases for a planner failure caused by units already installed.
    _CONFLICT_MARKERS = (
        "conflicting dependency",
        "only one of the following can be installed at once",
        "cannot satisfy dependency",
        "cannot complete the install because",
    )

    def install(self, request: InstallRequest) -> InstallResult:
        launcher = self._resolve_launcher(request.s32ds_path)
        if launcher is None:
            return self._result(
                request,
                InstallStatus.REQUIRES_EXISTING_S32DS,
                error=(
                    "No S32 Design Studio installation is available to install into. "
                    "Install S32 Design Studio first, or pass s32ds_path pointing at "
                    "an existing installation."
                ),
            )

        resolved, suggestions = resolve_source(
            request.repository,
            component=request.component,
            provider_location=self.REPOSITORY,
            lookup=self.source_lookup,
        )
        if not resolved.repository:
            status = (
                InstallStatus.REQUIRES_SOURCE
                if resolved.origin is InstallSource.UNRESOLVED
                else InstallStatus.FAILED
            )
            return self._result(
                request,
                status,
                suggested_sources=suggestions,
                error=(
                    f"{resolved.error or 'No p2 update site provided.'} Pass the "
                    "update-site zip, folder, or URL."
                ),
            )
        # A remote update site needs no download: the director reads it in place.
        repository = resolved.repository

        iu_ids = tuple(request.iu_ids) or self.IU_IDS
        discovered: tuple[str, ...] = ()
        if not iu_ids:
            discovered, error = self.discover_root_ius(launcher, repository)
            if error:
                return self._result(request, InstallStatus.FAILED, error=error)
            if not discovered:
                return self._result(
                    request,
                    InstallStatus.FAILED,
                    error=(
                        f"No installable root features found in {repository}. Pass "
                        "iu_ids explicitly if the site publishes none."
                    ),
                )
            if request.select_ius:
                return self._result(
                    request,
                    InstallStatus.REQUIRES_SELECTION,
                    candidates=discovered,
                    error=(
                        f"{repository} publishes {len(discovered)} installable "
                        "products. Choose the ones to install and pass them as "
                        "iu_ids."
                    ),
                )
            # Everything the site publishes. An NXP update site carries the
            # products that belong together, so installing all of them is the
            # expected outcome; a caller wanting less says so with iu_ids or
            # asks for the list with select_ius.
            iu_ids = discovered

        result = self._install_ius(request, launcher, repository, iu_ids)
        if discovered and result.status is InstallStatus.REQUIRES_CONFIRMATION:
            result = dataclasses.replace(
                result,
                candidates=discovered,
                error=(
                    f"{repository} publishes {len(discovered)} features: "
                    f"{', '.join(discovered)}. Retry with confirm=true to install "
                    "them all, or pass iu_ids to install a subset."
                ),
            )
        elif discovered and result.status is not InstallStatus.FAILED:
            note = (
                f"Installed all {len(discovered)} features published by the "
                f"site: {', '.join(discovered)}."
            )
            result = dataclasses.replace(
                result, guidance=f"{result.guidance} {note}".strip()
            )
        if result.status is InstallStatus.FAILED and self._is_conflict(result):
            result = self._retry_missing_only(request, launcher, repository, iu_ids, result)
        return result

    def _install_ius(
        self,
        request: InstallRequest,
        launcher: Path,
        repository: str,
        iu_ids: Sequence[str],
    ) -> InstallResult:
        command = [
            str(launcher),
            "-application",
            self.P2_APPLICATION,
            "-repository",
            repository,
            "-installIU",
            ",".join(iu_ids),
            "-noSplash",
        ]
        result = self._run(request, command, installed_path=str(launcher.parent))
        return dataclasses.replace(result, installed_ius=tuple(iu_ids) if result.success else ())

    # -- conflict recovery --------------------------------------------------

    @classmethod
    def _is_conflict(cls, result: InstallResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return any(marker in text for marker in cls._CONFLICT_MARKERS)

    def _retry_missing_only(
        self,
        request: InstallRequest,
        launcher: Path,
        repository: str,
        iu_ids: Sequence[str],
        failed: InstallResult,
    ) -> InstallResult:
        """Retry once with the units S32DS does not already have.

        p2 rolls back a failed install, so the first attempt left nothing behind.
        The retry is never followed by another one.
        """

        first_error = f"{failed.error} {_tail(failed.stderr) or _tail(failed.stdout)}".strip()
        installed = self.list_installed_ius(launcher)
        listing, _ = self._list_site(launcher, repository)
        site_versions = self._parse_unit_versions(listing)
        missing: list[str] = []
        skipped: list[SkippedUnit] = []
        for iu in iu_ids:
            hit = installed.get(iu)
            if hit is None:
                missing.append(iu)
            else:
                skipped.append(
                    SkippedUnit(
                        id=iu,
                        site_version=site_versions.get(iu, ""),
                        installed_version=hit[0],
                        found_by=hit[1],
                    )
                )

        if not skipped or not missing:
            reason = (
                "every requested feature is already installed"
                if not missing
                else "none of the requested features is installed yet, so the "
                "conflict is not caused by an existing feature"
            )
            return dataclasses.replace(
                failed,
                skipped_ius=tuple(skipped),
                first_attempt_error=first_error,
                guidance=f"{failed.guidance} Conflict detected; no retry because {reason}.".strip(),
            )

        retry = self._install_ius(
            dataclasses.replace(request, confirm=True), launcher, repository, missing
        )
        summary = (
            f"Full install conflicted with features already in S32DS; retried once "
            f"with the {len(missing)} missing features and skipped {len(skipped)}: "
            + ", ".join(
                f"{unit.id} (site {unit.site_version or '?'}, installed "
                f"{unit.installed_version or '?'}, via {unit.found_by})"
                for unit in skipped
            )
            + "."
        )
        if not retry.success:
            summary += " The retry failed as well; not retrying again."
        return dataclasses.replace(
            retry,
            skipped_ius=tuple(skipped),
            retried_missing_only=True,
            first_attempt_error=first_error,
            guidance=f"{retry.guidance} {summary}".strip(),
        )

    def list_installed_ius(self, launcher: Path) -> dict[str, tuple[str, str]]:
        """Installed feature groups as ``{iu_id: (version, found_by)}``.

        ``-listInstalledRoots`` lists only top-level roots, so features pulled in
        by a larger feature are added from ``eclipse/features``.
        """

        installed: dict[str, tuple[str, str]] = {}
        command = [
            str(launcher),
            "-application",
            self.P2_APPLICATION,
            "-listInstalledRoots",
            "-noSplash",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._DISCOVERY_TIMEOUT_S,
                check=False,
            )
            installed.update(self._parse_installed_roots(completed.stdout))
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("listInstalledRoots failed: %s", exc)

        for iu, version in self._scan_features_folder(launcher.parent / "features").items():
            installed.setdefault(iu, (version, "features_folder"))
        return installed

    @classmethod
    def _parse_installed_roots(cls, listing: str) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}
        for line in (listing or "").splitlines():
            match = cls._INSTALLED_LINE.match(line.strip())
            if match is not None:
                found[match.group("id")] = (match.group("version"), "installed_roots")
        return found

    @classmethod
    def _scan_features_folder(cls, folder: Path) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            entries = list(folder.iterdir())
        except OSError:
            return found
        for entry in entries:
            match = cls._FEATURE_DIR.match(entry.name)
            if match is None:
                continue
            iu = f"{match.group('id')}{cls._FEATURE_GROUP_SUFFIX}"
            version = match.group("version")
            if iu not in found or version > found[iu]:
                found[iu] = version
        return found

    # -- installable-unit discovery ----------------------------------------

    def discover_root_ius(
        self, launcher: Path, repository: str
    ) -> tuple[tuple[str, ...], str]:
        """List the installable root features published by an update site.

        Runs the director in ``-list`` mode and maps each root-feature marker to
        the unit that can actually be installed. Returns
        ``(ids, error_message)``.
        """

        listing, error = self._list_site(launcher, repository)
        if error:
            return (), error
        return self._parse_root_ius(listing), ""

    def _list_site(self, launcher: Path, repository: str) -> tuple[str, str]:
        """Run ``-list`` on a site. Returns ``(stdout, error)``.

        Stateless because providers are shared across concurrent requests.
        """

        command = [
            str(launcher),
            "-application",
            self.P2_APPLICATION,
            "-repository",
            repository,
            "-list",
            "-noSplash",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._DISCOVERY_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "", f"Listing {repository} timed out."
        except OSError as exc:
            return "", f"Could not list {repository}: {exc}"

        if completed.returncode != 0:
            return "", (
                f"Listing {repository} failed with exit code {completed.returncode}. "
                f"{_tail(completed.stderr) or _tail(completed.stdout)}"
            )
        return completed.stdout or "", ""

    @classmethod
    def _parse_unit_versions(cls, listing: str) -> dict[str, str]:
        versions: dict[str, str] = {}
        for line in (listing or "").splitlines():
            match = cls._IU_LINE.match(line.strip())
            if match is not None:
                versions[match.group("id")] = match.group("version")
        return versions

    @classmethod
    def _parse_root_ius(cls, listing: str) -> tuple[str, ...]:
        """Extract the installable root features from ``-list`` output.

        The listing contains many units that must not be installed: categories,
        ``a.jre.javase``, ``.feature.jar`` artifacts, platform fragments. When
        the site carries ``.root.feature_root`` markers, each maps to a
        ``.root.feature.feature.group`` sibling, whose presence is verified
        before it is offered - a constructed id that the site does not publish
        would fail confusingly at the director. Without such markers every
        ``.feature.group`` unit is returned.
        """

        units: list[str] = []
        markers: list[str] = []
        for line in (listing or "").splitlines():
            match = cls._IU_LINE.match(line.strip())
            if match is None:
                continue
            unit_id = match.group("id")
            if unit_id not in units:
                units.append(unit_id)
            if unit_id.endswith(cls._ROOT_MARKER_SUFFIX):
                markers.append(unit_id)

        if not markers:
            return tuple(u for u in units if u.endswith(cls._FEATURE_GROUP_SUFFIX))

        installable: list[str] = []
        for marker in markers:
            prefix = marker[: -len(cls._ROOT_MARKER_SUFFIX)]
            candidate = f"{prefix}{cls._INSTALLABLE_SUFFIX}"
            if candidate in units and candidate not in installable:
                installable.append(candidate)
            elif candidate not in units:
                logger.warning(
                    "Update site marks %s as a root feature but does not publish %s; "
                    "skipping it.",
                    marker,
                    candidate,
                )
        return tuple(installable)

    # -- resolution helpers -------------------------------------------------

    def _resolve_launcher(self, s32ds_path: str | None) -> Path | None:
        """Find the headless S32DS launcher under an installation root.

        Accepts either the installation root or its ``eclipse`` subfolder. With
        no root supplied the machine is scanned, and a single installation is
        used. Several are not, because picking one would be a guess - the caller
        then gets ``REQUIRES_EXISTING_S32DS`` and asks the user, which is also
        what happens when nothing is found at all.
        """

        if not s32ds_path:
            detected = DETECTORS.find_all(S32DS_COMPONENT)
            if len(detected) != 1:
                return None
            s32ds_path = detected[0].root
        try:
            root = Path(s32ds_path).resolve(strict=False)
        except (OSError, ValueError):
            return None

        for base in (root, root / "eclipse"):
            for name in self._LAUNCHER_NAMES:
                candidate = base / name
                if candidate.is_file():
                    return candidate
        logger.debug("No p2 launcher found under %s", root)
        return None


class GuidanceProvider(InstallProvider):
    """Component that cannot be installed automatically.

    Subclasses only declare ``DOWNLOAD_URL`` and ``NOTES``; the result carries
    them so the agent can direct the user.
    """

    METHOD = InstallMethod.GUIDANCE_ONLY

    def install(self, request: InstallRequest) -> InstallResult:
        guidance = self.NOTES or (
            f"{self.DISPLAY_NAME or self.PROVIDER} must be installed manually."
        )
        if self.DOWNLOAD_URL:
            guidance = f"{guidance} Download: {self.DOWNLOAD_URL}"
        return self._result(request, InstallStatus.GUIDANCE_ONLY, guidance=guidance)


def _escape_install_dir(path: str) -> str:
    """Escape a target directory for an InstallAnywhere properties file.

    Backslashes must be doubled on Windows; POSIX paths are written verbatim.
    An already-escaped value is left alone so callers can pass either form.
    """

    if "\\" not in path:
        return path
    if "\\\\" in path:
        return path
    return path.replace("\\", "\\\\")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove temporary properties file %s: %s", path, exc)
