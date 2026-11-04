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

"""Install-source resolution, fetching and p2 repository normalisation.

Users hand us whatever they have: a product installer, a folder containing one,
a zipped Eclipse update site, a directory update site, or a URL for any of
those. These helpers turn any of them into the exact form the underlying tool
expects, so no provider - and no agent - has to hand-build a URI.

Two things deliberately differ between the methods:

* A p2 update site may stay remote. The director accepts
  ``-repository https://...`` directly, so nothing is downloaded.
* A product installer must exist on disk, so a remote one is fetched to a
  temporary file first.

p2 repository forms produced here:

* zipped update site   -> ``jar:file:<abs path>!/``  (NXP zips are flat, so the
  archive root is the site root)
* directory update site -> ``file:<abs path>``
* anything already a URI -> passed through untouched
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

# Schemes we treat as "already a location" and never rewrite into a path.
_REMOTE_PREFIXES = ("http://", "https://")
_URI_PREFIXES = ("jar:", "file:", *_REMOTE_PREFIXES)

# Installer executable extension per platform. Any file with the right extension
# is accepted: product installers are not named consistently across releases
# (e.g. SW32_S32DS_3.6.10_RFP_D2607_win32.x86_64.exe), so matching on the
# product name would reject legitimate drops.
_INSTALLER_SUFFIX = {"windows": ".exe", "linux": ".bin", "darwin": ".dmg"}

# Files an Eclipse p2 site carries at its root. Composite sites publish the
# composite* variants and p2.index instead of plain content/artifacts.
_SITE_MARKERS = (
    "p2.index",
    "content.jar",
    "content.xml",
    "artifacts.jar",
    "artifacts.xml",
    "compositeContent.jar",
    "compositeContent.xml",
    "compositeArtifacts.jar",
    "compositeArtifacts.xml",
)

_DOWNLOAD_TIMEOUT_S = 1800.0
_DOWNLOAD_CHUNK = 1 << 20

# Returns candidate download locations for a component, e.g. from documentation.
# Injected by the server so this package stays free of knowledge-base imports.
SourceLookup = Callable[[str], Sequence[str]]


class ArtifactKind(StrEnum):
    """Shape of a user-supplied install artifact."""

    EXE = "exe"                      # a product installer executable
    INSTALLER_DIR = "installer_dir"  # a folder containing one
    P2_ZIP = "p2_zip"                # a zipped Eclipse update site
    P2_DIR = "p2_dir"                # an unpacked update site
    REMOTE_REPOSITORY = "remote_repository"  # a URL serving an update site
    REMOTE_FILE = "remote_file"      # a URL serving an installer or site zip
    UNKNOWN = "unknown"


class InstallSource(StrEnum):
    """Where a resolved location came from. Reported so the user can judge it."""

    EXPLICIT = "explicit"        # the caller supplied it
    PROVIDER = "provider"        # a provider constant
    DOCUMENTATION = "documentation"  # looked up; a suggestion, not a fact
    UNRESOLVED = "unresolved"    # nothing known; ask the user


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """Outcome of resolving one artifact location."""

    kind: ArtifactKind
    path: Path | None = None
    # Populated for p2 artifacts: the normalised -repository argument.
    repository: str = ""
    # Populated for remote artifacts before they are fetched.
    url: str = ""
    origin: InstallSource = InstallSource.EXPLICIT
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def is_remote(self) -> bool:
        return self.kind in (ArtifactKind.REMOTE_REPOSITORY, ArtifactKind.REMOTE_FILE)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of downloading a remote artifact."""

    path: Path | None = None
    url: str = ""
    bytes_written: int = 0
    elapsed_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.path is not None


def is_remote(location: str | Path) -> bool:
    return str(location).strip().lower().startswith(_REMOTE_PREFIXES)


def normalise_repository(location: str | Path) -> str:
    """Return the ``-repository`` argument for a p2 update site.

    Accepts a zip, a directory, or an already-formed URI. Raises ``ValueError``
    when a local path does not exist, since a silently wrong repository only
    surfaces as a confusing director failure much later.
    """

    text = str(location).strip()
    if text.startswith(_URI_PREFIXES):
        return text

    path = Path(text).resolve(strict=False)
    if not path.exists():
        raise ValueError(f"Update site not found: {path}")

    if path.is_dir():
        return path.as_uri()
    if path.suffix.lower() == ".zip":
        # NXP update-site zips are flat, so the archive root is the site root.
        return f"jar:{path.as_uri()}!/"
    raise ValueError(
        f"Not a p2 update site: {path}. Expected a directory or a .zip update site."
    )


def installer_suffix(platform: str) -> str:
    return _INSTALLER_SUFFIX.get(platform, ".exe")


def resolve_installer(location: str | Path, *, platform: str) -> ResolvedSource:
    """Locate a product installer executable on disk.

    ``location`` may be the executable itself or a directory containing exactly
    one candidate. An ambiguous directory is reported rather than guessed.
    """

    suffix = installer_suffix(platform)
    try:
        path = Path(str(location)).resolve(strict=False)
    except (OSError, ValueError) as exc:
        return ResolvedSource(ArtifactKind.UNKNOWN, error=f"Invalid path: {exc}")

    if not path.exists():
        return ResolvedSource(ArtifactKind.UNKNOWN, error=f"Path not found: {path}")

    if path.is_file():
        if path.suffix.lower() != suffix:
            return ResolvedSource(
                ArtifactKind.UNKNOWN,
                error=(
                    f"{path.name} is not an installer for this platform "
                    f"(expected a {suffix} file)."
                ),
            )
        return ResolvedSource(ArtifactKind.EXE, path=path)

    candidates = sorted(
        child for child in path.iterdir() if child.is_file() and child.suffix.lower() == suffix
    )
    if not candidates:
        return ResolvedSource(
            ArtifactKind.UNKNOWN, error=f"No {suffix} installer found in {path}."
        )
    if len(candidates) > 1:
        names = ", ".join(candidate.name for candidate in candidates)
        return ResolvedSource(
            ArtifactKind.UNKNOWN,
            error=(
                f"{path} contains several {suffix} files ({names}). "
                "Point at the installer explicitly."
            ),
        )
    return ResolvedSource(ArtifactKind.INSTALLER_DIR, path=candidates[0])


def classify_artifact(location: str | Path, *, platform: str | None = None) -> ResolvedSource:
    """Determine what kind of install artifact a location is.

    Lets a caller say "install from <location>" without also stating whether it
    is an installer or an update site, or whether it is local or remote.
    """

    platform = platform or ("windows" if os.name == "nt" else "linux")
    text = str(location).strip()

    if text.lower().startswith(_REMOTE_PREFIXES):
        # A URL ending in .zip has to be downloaded before we can tell what it
        # is; anything else is assumed to be a served update site, which the p2
        # director can read in place.
        if text.lower().endswith(".zip") or text.lower().endswith(installer_suffix(platform)):
            return ResolvedSource(ArtifactKind.REMOTE_FILE, url=text)
        return ResolvedSource(ArtifactKind.REMOTE_REPOSITORY, url=text, repository=text)

    if text.startswith(("jar:", "file:")):
        return ResolvedSource(ArtifactKind.P2_DIR, repository=text)

    try:
        path = Path(text).resolve(strict=False)
    except (OSError, ValueError) as exc:
        return ResolvedSource(ArtifactKind.UNKNOWN, error=f"Invalid path: {exc}")

    if not path.exists():
        return ResolvedSource(ArtifactKind.UNKNOWN, error=f"Path not found: {path}")

    if path.is_file() and path.suffix.lower() == ".zip":
        return ResolvedSource(
            ArtifactKind.P2_ZIP, path=path, repository=normalise_repository(path)
        )

    if path.is_dir() and looks_like_update_site(path):
        return ResolvedSource(
            ArtifactKind.P2_DIR, path=path, repository=normalise_repository(path)
        )

    return resolve_installer(path, platform=platform)


def resolve_source(
    location: str | Path | None,
    *,
    component: str = "",
    provider_location: str = "",
    lookup: SourceLookup | None = None,
    platform: str | None = None,
) -> tuple[ResolvedSource, tuple[str, ...]]:
    """Find something installable, in decreasing order of trust.

    1. the location the caller gave us
    2. a location the provider declares
    3. locations suggested by ``lookup`` (e.g. documentation)
    4. nothing - the caller must ask the user

    Returns ``(resolved, suggestions)``. Suggestions from step 3 are never used
    automatically: a location read out of prose may be stale or wrong, so the
    user picks. Step 3 therefore only ever populates the suggestion list.
    """

    if location:
        resolved = classify_artifact(location, platform=platform)
        return dataclasses.replace(resolved, origin=InstallSource.EXPLICIT), ()

    if provider_location:
        resolved = classify_artifact(provider_location, platform=platform)
        return dataclasses.replace(resolved, origin=InstallSource.PROVIDER), ()

    suggestions: tuple[str, ...] = ()
    if lookup is not None and component:
        try:
            suggestions = tuple(dict.fromkeys(str(item) for item in lookup(component) if item))
        except Exception as exc:  # noqa: BLE001 - a failing lookup must not break the install
            logger.warning("Source lookup for %r failed: %s", component, exc)

    return (
        ResolvedSource(
            ArtifactKind.UNKNOWN,
            origin=InstallSource.UNRESOLVED,
            error=(
                f"No location known for {component or 'this component'}. Provide a local "
                "path or a URL."
            ),
        ),
        suggestions,
    )


def fetch_to_temp(
    url: str,
    *,
    timeout_s: float = _DOWNLOAD_TIMEOUT_S,
    suggested_name: str = "",
) -> FetchResult:
    """Download a remote artifact to a temporary file.

    Streams rather than buffering, since product installers are large. Proxy
    settings are taken from the environment. Authentication is not handled: an
    SSO-protected download returns a clear message telling the user to fetch it
    manually and pass the path.
    """

    import httpx

    name = suggested_name or Path(url.split("?", 1)[0]).name or "download"
    suffix = Path(name).suffix or ".bin"
    start = time.monotonic()
    handle = tempfile.NamedTemporaryFile(
        prefix="nxp_download_", suffix=suffix, delete=False
    )
    target = Path(handle.name)
    written = 0
    try:
        with handle:
            with httpx.stream(
                "GET", url, timeout=timeout_s, follow_redirects=True
            ) as response:
                if response.status_code in (401, 403):
                    return FetchResult(
                        url=url,
                        error=(
                            f"{url} requires authentication ({response.status_code}). "
                            "Download it manually and pass the local path instead."
                        ),
                    )
                response.raise_for_status()
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
    except httpx.HTTPStatusError as exc:
        _unlink_quietly(target)
        return FetchResult(
            url=url, error=f"Download failed with HTTP {exc.response.status_code}: {url}"
        )
    except httpx.HTTPError as exc:
        _unlink_quietly(target)
        return FetchResult(url=url, error=f"Could not download {url}: {exc}")
    except OSError as exc:
        _unlink_quietly(target)
        return FetchResult(url=url, error=f"Could not write the download: {exc}")

    if written == 0:
        _unlink_quietly(target)
        return FetchResult(url=url, error=f"{url} returned an empty response.")

    return FetchResult(
        path=target,
        url=url,
        bytes_written=written,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


def looks_like_update_site(path: Path) -> bool:
    """An Eclipse p2 site carries its metadata at the root."""

    return any((path / marker).is_file() for marker in _SITE_MARKERS)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)
