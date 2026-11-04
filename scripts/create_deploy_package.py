#!/usr/bin/env python3
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

"""Create a self-installing MCP Core deployment package.

This script collects all required WHL files, bundled assets (knowledge_db,
models, skills, configs), and the installer entry point into a single runnable
.pyz file using Python's zipapp module.

Wheel sources, searched in this order:
    1. dist/*.whl                 wheels built from this repository by
                                  scripts/build_modules.py
    2. --whl-dir DIR (repeatable) any folder holding pre-built wheels, for
                                  example wheels downloaded by hand from
                                  another repository's release
    3. --fetch-external           wheels declared in the root pyproject.toml
                                  under [[tool.nxp-mcp.external]], downloaded
                                  from that GitHub release (exact tag) into
                                  dist/external/<owner>__<repo>/<tag>/

Every required package must resolve to exactly one wheel. The build stops when:
    - a required package has no wheel,
    - two different wheels are found for the same package,
    - an external wheel does not match its "==" pin in the root pyproject.toml,
    - a bundled wheel requires a version of another bundled nxp-mcp package
      that is not the one being shipped (pip would fail on the target).

Usage:
    python scripts/create_deploy_package.py
    python scripts/create_deploy_package.py --fetch-external
    python scripts/create_deploy_package.py --whl-dir C:/tmp/mcp-core-2.0.0
    python scripts/create_deploy_package.py --variant extended --fetch-external
    python scripts/create_deploy_package.py --output dist/mcp-core-installer.pyz

Private GitHub repositories (and higher API rate limits) need a token in the
GITHUB_TOKEN or GH_TOKEN environment variable. HTTPS_PROXY is honoured.
Without --fetch-external the script makes no network access at all.

Output:
    dist/mcp-core-installer-<version>.pyz

The resulting .pyz can be distributed as a single file and run with:
    python mcp-core-installer-<version>.pyz
    python mcp-core-installer-<version>.pyz --dest /path/to/install
    python mcp-core-installer-<version>.pyz --yes
    python mcp-core-installer-<version>.pyz --dest /path/to/install --no-venv

By default the bundled installer creates a .venv inside the destination
directory and installs the wheels into it (handled at install time by
scripts/installer/__main__.py); the run-mcp.sh / run-mcp.bat wrappers
auto-activate that .venv. This is required on PEP 668 "externally-managed" Linux
distributions (Ubuntu 24.04, Debian 12+, Fedora, ...) where system-wide pip
installs are blocked. Pass --no-venv to install into the system Python instead.
"""


import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

# --- Paths -------------------------------------------------------------------

PROJECT_ROOT  = Path(__file__).parent.parent
INSTALLER_DIR = PROJECT_ROOT / "scripts" / "installer"
DIST_DIR      = PROJECT_ROOT / "dist"

# Download cache for external wheels. It is a sub-folder on purpose: CI
# uploads dist/*.whl to the package index, and wheels built by another
# repository must not be re-published as if they were built here.
EXTERNAL_CACHE_DIR = DIST_DIR / "external"

# Supported variants
SUPPORTED_VARIANTS = ["standard", "extended"]
DEFAULT_VARIANT = "standard"

# Supported [[tool.nxp-mcp.external]] source kinds.
SUPPORTED_SOURCES = ("github-release",)

GITHUB_API = "https://api.github.com"
USER_AGENT = "nxp-mcp-create-deploy-package"


# --- Helpers -----------------------------------------------------------------
# Package-name / wheel-name utilities live in the shared manifest module and are
# accessed via the imported manifest (manifest.normalize, manifest.whl_dist_name,
# manifest.whl_version, manifest.required_whl_names) so there is a single
# implementation shared with the installer.

def fail(message: str) -> NoReturn:
    """Print an error and stop the build."""
    print(f"ERROR: {message}")
    sys.exit(1)


def load_manifest(variant: str):
    """Dynamically import the appropriate manifest module based on variant.

    Returns the manifest module (_manifest or _manifest_extended).
    """
    sys.path.insert(0, str(INSTALLER_DIR))

    if variant == "standard":
        import _manifest
        return _manifest
    elif variant == "extended":
        import _manifest_extended
        return _manifest_extended
    else:
        print(f"ERROR: Unknown variant '{variant}'. Choose from: {', '.join(SUPPORTED_VARIANTS)}")
        sys.exit(1)


def load_pyproject() -> dict:
    """Return the parsed root pyproject.toml."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        fail(f"Root pyproject.toml not found: {pyproject}")
    with open(pyproject, "rb") as fh:
        return tomllib.load(fh)


def resolve_version(override: str | None, pyproject: dict) -> str:
    """Return the version to stamp into the installer.

    If --version was passed on the CLI, use it verbatim.
    Otherwise fall back to the [project].version field in the root
    pyproject.toml so the installer version always tracks the package release.
    """
    if override:
        return override.strip()
    version = pyproject.get("project", {}).get("version")
    if not version:
        fail("[project].version not found in pyproject.toml")
    return version


def build_manifest_bytes(version: str, manifest_src: Path) -> bytes:
    """Return the manifest file bytes with @@VERSION@@ substituted.

    Args:
        version: Version string to substitute
        manifest_src: Path of the manifest file (_manifest.py or _manifest_extended.py)
    """
    if not manifest_src.exists():
        print(f"ERROR: {manifest_src} not found.")
        sys.exit(1)

    text = manifest_src.read_text(encoding="utf-8")
    patched = text.replace('"@@VERSION@@"', f'"{version}"')
    return patched.encode("utf-8")


def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _sha256(path: Path) -> str:
    """Return the hex sha256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Version checks ----------------------------------------------------------

_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,]+)")
_REQ_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?\s*([^)]*?)\s*\)?$"
)


def root_pins(pyproject: dict, manifest) -> dict[str, str]:
    """Return {normalised name: version} for each '==' pin in [project].dependencies."""
    pins: dict[str, str] = {}
    for req in pyproject.get("project", {}).get("dependencies", []):
        match = _PIN_RE.match(req)
        if match:
            pins[manifest.normalize(match.group(1))] = match.group(2)
    return pins


def _same_version(a: str, b: str) -> bool:
    """Compare two versions, using 'packaging' when available."""
    try:
        from packaging.version import Version
        return Version(a) == Version(b)
    except Exception:
        return a.strip().lower().lstrip("v") == b.strip().lower().lstrip("v")


def _satisfies(version: str, spec: str) -> bool | None:
    """Return whether `version` satisfies `spec`, or None if it cannot be told.

    Uses 'packaging' (installed together with 'build') for full specifier
    support; without it only exact '==' specifiers are evaluated.
    """
    if not spec:
        return True
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:
        match = re.fullmatch(r"===?\s*([^\s*]+)", spec)
        return _same_version(version, match.group(1)) if match else None
    try:
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return None


def _wheel_requires(path: Path) -> list[str]:
    """Return the Requires-Dist entries of a wheel, skipping optional extras."""
    with zipfile.ZipFile(path) as zf:
        meta = next(
            (n for n in zf.namelist()
             if n.endswith(".dist-info/METADATA") and n.count("/") == 1),
            None,
        )
        if meta is None:
            return []
        text = zf.read(meta).decode("utf-8", "replace")
    requires: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            break  # end of the metadata headers; the rest is the description
        if line.lower().startswith("requires-dist:"):
            req, _, marker = line.split(":", 1)[1].partition(";")
            if "extra" not in marker:
                requires.append(req.strip())
    return requires


def check_bundle_dependencies(selected: dict, manifest, unresolved=()) -> list[str]:
    """Check that the bundled wheels agree on each other's versions.

    The installer runs pip with --find-links pointing at the bundled wheels,
    and nxp-mcp packages are not on any public index, so every requirement
    on another nxp-mcp package must be met by the wheel shipped in the .pyz.
    Packages in `unresolved` already have their own error (missing or
    ambiguous) and are skipped here so that one cause is reported once.
    Returns a list of error messages (empty when consistent).
    """
    errors: list[str] = []
    versions = {name: manifest.whl_version(p) for name, (p, _) in selected.items()}
    for name, (path, origin) in sorted(selected.items()):
        for req in _wheel_requires(path):
            match = _REQ_RE.match(req)
            if not match:
                continue
            dep = manifest.normalize(match.group(1))
            if not dep.startswith("nxp-mcp") or dep in unresolved:
                continue
            if dep not in versions:
                errors.append(f"{name} [{origin}] requires '{req}', which is not bundled.")
            elif _satisfies(versions[dep], match.group(2)) is False:
                errors.append(
                    f"{name} [{origin}] requires '{req}', but the bundled "
                    f"{dep} is {versions[dep]}."
                )
    return errors


# --- External wheel sources --------------------------------------------------
# Declared in the root pyproject.toml, one block per external repository:
#
#   [[tool.nxp-mcp.external]]
#   source = "github-release"
#   repo = "owner/name"
#   tag = "v2.0.0"
#   packages = ["nxp-mcp-shared", ...]
#
# New source kinds (for example a package index) are added by extending
# SUPPORTED_SOURCES and the dispatch in fetch_external().

def external_sources(pyproject: dict, manifest) -> list[dict]:
    """Return the validated [[tool.nxp-mcp.external]] entries."""
    entries = pyproject.get("tool", {}).get("nxp-mcp", {}).get("external", [])
    sources: list[dict] = []
    owner_of: dict[str, str] = {}
    for index, entry in enumerate(entries, 1):
        where = f"[[tool.nxp-mcp.external]] entry #{index}"
        kind = entry.get("source")
        repo = str(entry.get("repo", "")).strip()
        tag = str(entry.get("tag", "")).strip()
        packages = entry.get("packages") or []
        if kind not in SUPPORTED_SOURCES:
            fail(f"{where}: unsupported source {kind!r} "
                 f"(supported: {', '.join(SUPPORTED_SOURCES)})")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            fail(f"{where}: 'repo' must be 'owner/name', got {repo!r}")
        if not tag:
            fail(f"{where}: 'tag' must be an exact release tag")
        if not isinstance(packages, list) or not all(
            isinstance(p, str) and p.strip() for p in packages
        ) or not packages:
            fail(f"{where}: 'packages' must be a non-empty list of package names")
        label = f"{repo}@{tag}"
        names = [manifest.normalize(p.strip()) for p in packages]
        for name in names:
            if name in owner_of:
                fail(f"{where}: {name} is already declared by {owner_of[name]}")
            owner_of[name] = label
        sources.append({"source": kind, "repo": repo, "tag": tag,
                        "packages": names, "label": label})
    return sources


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that does not leak the GitHub token to another host.

    Release asset downloads redirect to a signed storage URL. urllib copies
    the request headers onto the redirected request, so the Authorization
    header is removed whenever the redirect leaves the original host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (
            urllib.parse.urlsplit(newurl).netloc
            != urllib.parse.urlsplit(req.full_url).netloc
        ):
            new.remove_header("Authorization")
        return new


def _github_open(url: str, accept: str, timeout: int):
    """Open a GitHub URL with the standard headers and the optional token."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_DropAuthOnRedirect)
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403, 404):
            hint = (" Check 'repo' and 'tag' in [[tool.nxp-mcp.external]]; "
                    "private repositories need GITHUB_TOKEN or GH_TOKEN.")
        raise RuntimeError(f"GitHub returned HTTP {exc.code} for {url}.{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error contacting GitHub ({url}): {exc.reason}") from exc


def _asset_ok(path: Path, asset: dict) -> bool:
    """Return True when `path` matches the published size and sha256 of `asset`."""
    if not path.is_file():
        return False
    size = asset.get("size")
    if isinstance(size, int) and path.stat().st_size != size:
        return False
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:") and _sha256(path) != digest[len("sha256:"):].lower():
        return False
    return True


def _safe_dirname(text: str) -> str:
    """Make a repo or tag usable as a single folder name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def fetch_github_release(src: dict, manifest) -> list[Path]:
    """Download the declared wheels of one GitHub release into the cache.

    Only .whl assets of the listed packages are taken, from the exact tag.
    Files already cached with the published size and sha256 are reused.
    Returns the paths of the wheels for the declared packages.
    """
    cache = (EXTERNAL_CACHE_DIR
             / _safe_dirname(src["repo"].replace("/", "__"))
             / _safe_dirname(src["tag"]))
    url = (f"{GITHUB_API}/repos/{src['repo']}/releases/tags/"
           f"{urllib.parse.quote(src['tag'], safe='')}")
    with _github_open(url, "application/vnd.github+json", timeout=30) as resp:
        release = json.loads(resp.read().decode("utf-8"))
    wheels = [a for a in release.get("assets") or []
              if str(a.get("name", "")).endswith(".whl")]

    print(f"Fetching external wheels from {src['label']} -> {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for package in src["packages"]:
        matches = [a for a in wheels if manifest.whl_dist_name(a["name"]) == package]
        if len(matches) != 1:
            found = ", ".join(a["name"] for a in matches) or "none"
            raise RuntimeError(f"Release {src['label']} must contain exactly one "
                               f"wheel for {package} (found: {found}).")
        asset = matches[0]
        target = cache / asset["name"]
        paths.append(target)
        if _asset_ok(target, asset):
            print(f"  = {asset['name']}  (cached)")
            continue
        print(f"  + {asset['name']}  ({format_size(int(asset.get('size') or 0))})")
        partial = target.with_suffix(".part")
        with _github_open(asset["url"], "application/octet-stream", timeout=120) as resp, \
                open(partial, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if not _asset_ok(partial, asset):
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{asset['name']} failed size/sha256 verification "
                               f"after download from {src['label']}.")
        partial.replace(target)
        if not str(asset.get("digest") or "").startswith("sha256:"):
            print("    (note: no sha256 digest published; verified by size only)")
    return paths


def fetch_external(src: dict, manifest) -> list[Path]:
    """Dispatch one external source to its fetcher."""
    if src["source"] == "github-release":
        return fetch_github_release(src, manifest)
    fail(f"unsupported source {src['source']!r}")


# --- Wheel collection --------------------------------------------------------

def collect_whl_files(
    manifest,
    sources: list[tuple[str, list[Path]]],
    externals: dict[str, str],
    pins: dict[str, str],
) -> dict[str, tuple[Path, str]]:
    """Resolve every package required by COMPONENTS to exactly one wheel.

    sources:   (origin label, wheel paths) pairs, in search order
    externals: {normalised name: "owner/repo@tag"} for packages declared in
               [[tool.nxp-mcp.external]]
    pins:      exact '==' pins from the root pyproject.toml

    Returns {normalised name: (wheel path, origin label)}. Exits on any error.
    """
    required: set[str] = manifest.required_whl_names(manifest.COMPONENTS)

    candidates: dict[str, list[tuple[Path, str]]] = {}
    seen: set[Path] = set()
    for origin, paths in sources:
        for path in paths:
            name = manifest.whl_dist_name(path)
            real = path.resolve()
            if name in required and real not in seen:
                seen.add(real)
                candidates.setdefault(name, []).append((path, origin))

    errors: list[str] = []
    warnings: list[str] = []
    selected: dict[str, tuple[Path, str]] = {}

    for name in sorted(required):
        found = candidates.get(name, [])
        if not found:
            if name in externals:
                errors.append(f"{name}: no wheel found. It is declared external "
                              f"({externals[name]}); run with --fetch-external "
                              f"or pass --whl-dir DIR.")
            else:
                errors.append(f"{name}: no wheel found. "
                              f"Run 'python scripts/build_modules.py'.")
            continue
        # Identical copies of one wheel in several places are fine; anything
        # else is ambiguous and must not be resolved by silent ordering.
        if len(found) > 1 and len({_sha256(p) for p, _ in found}) > 1:
            listing = "; ".join(f"{p.name} [{o}]" for p, o in found)
            errors.append(f"{name}: different wheels found: {listing}. Keep exactly "
                          f"one (e.g. 'python scripts/build_modules.py --clean', or "
                          f"remove packages/ sources now built in another repository).")
            continue
        selected[name] = found[0]

    # Packages declared external but still built in this repository (for
    # example a monorepo checkout that has not been split) are fine: they are
    # reported once and then treated like any other locally built wheel.
    built_locally = sorted(n for n, (_, o) in selected.items()
                           if n in externals and o == "dist")

    for name, (path, origin) in sorted(selected.items()):
        version = manifest.whl_version(path)
        pin = pins.get(name)
        if pin is None or _same_version(version, pin):
            continue
        message = (f"{name}: bundled {version} [{origin}] but the root "
                   f"pyproject.toml pins =={pin}.")
        # Only wheels brought in from outside this build (--whl-dir or
        # --fetch-external) must match the pin exactly. Locally built wheels
        # may carry a CI-stamped version (set_version.py); their consistency
        # is still enforced by check_bundle_dependencies() below.
        if origin != "dist":
            errors.append(message + " Align 'tag' in [[tool.nxp-mcp.external]] "
                                    "(or the --whl-dir content) and the pin.")
        else:
            warnings.append(message)

    errors += check_bundle_dependencies(selected, manifest,
                                        unresolved=required - set(selected))

    if built_locally:
        print(f"NOTE: built locally from packages/, not fetched: "
              f"{', '.join(built_locally)}")
    for message in warnings:
        print(f"WARNING: {message}")
    if errors:
        print("ERROR: Cannot assemble a consistent set of wheels:")
        for message in errors:
            print(f"         - {message}")
        sys.exit(1)
    if warnings or built_locally:
        print()
    return selected


# --- Assets ------------------------------------------------------------------

def add_component_assets(
    zf: zipfile.ZipFile,
    manifest
) -> int:
    """Add all component assets (files and folders) to the archive.

    Returns the total number of files added.
    """
    count = 0

    for comp_name, comp_def in manifest.COMPONENTS.items():
        # Add files
        for src_rel, dest_rel in comp_def.get("files", []):
            src = PROJECT_ROOT / src_rel
            if not src.exists():
                print(f"WARNING: Component '{comp_name}' file not found: {src}")
                continue
            zf.write(src, dest_rel)
            count += 1

        # Add folders
        #
        # A folder entry is a tuple:
        #   (source_folder, dest_folder)                 - copy whole tree
        #   (source_folder, dest_folder, [excludes])     - copy tree, skipping
        #                                                  the listed relative
        #                                                  subpaths
        # Excludes are relative to source_folder (POSIX-style). A file is
        # skipped when its path equals an exclude or lives under one, so
        # excluding "internal" drops both "internal" and "internal/foo/bar".
        for entry in comp_def.get("folders", []):
            src_rel, dest_prefix = entry[0], entry[1]
            excludes = entry[2] if len(entry) > 2 else []
            src_dir = PROJECT_ROOT / src_rel
            if not src_dir.exists():
                print(f"WARNING: Component '{comp_name}' folder not found: {src_dir}")
                continue
            for path in sorted(src_dir.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(src_dir)
                    rel_posix = rel.as_posix()
                    if any(
                        rel_posix == ex or rel_posix.startswith(ex + "/")
                        for ex in excludes
                    ):
                        continue
                    arcname = f"{dest_prefix}/{rel_posix}"
                    zf.write(path, arcname)
                    count += 1

    return count


# --- Main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build a self-installing .pyz package",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output .pyz path (default: dist/mcp-core-installer-<ver>.pyz)",
    )
    parser.add_argument(
        "--version",
        default=None,
        metavar="X.Y.Z",
        help=(
            "Version to stamp into the installer (default: read from "
            "[project].version in the root pyproject.toml)"
        ),
    )
    parser.add_argument(
        "--variant",
        default=DEFAULT_VARIANT,
        choices=SUPPORTED_VARIANTS,
        help=(
            f"Installer variant to build (default: {DEFAULT_VARIANT}). "
            "'standard' excludes internal-only components; "
            "'extended' includes everything."
        ),
    )
    parser.add_argument(
        "--whl-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Additional folder holding pre-built wheels; repeatable. "
            "Searched after dist/."
        ),
    )
    parser.add_argument(
        "--fetch-external",
        action="store_true",
        help=(
            "Download the wheels declared under [[tool.nxp-mcp.external]] in "
            "the root pyproject.toml (exact release tag) into dist/external/ "
            "and bundle them."
        ),
    )
    args = parser.parse_args()

    pyproject = load_pyproject()
    version = resolve_version(args.version, pyproject)
    manifest = load_manifest(args.variant)
    ext_sources = external_sources(pyproject, manifest)
    externals = {pkg: src["label"] for src in ext_sources for pkg in src["packages"]}

    # Resolve output path
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        DIST_DIR.mkdir(parents=True, exist_ok=True)
        if args.variant == DEFAULT_VARIANT:
            output_path = DIST_DIR / f"mcp-core-installer-{version}.pyz"
        else:
            output_path = DIST_DIR / f"mcp-core-installer-{args.variant}-{version}.pyz"

    print(f"Building MCP Core installer v{version}  [variant: {args.variant}]")
    print(f"Output: {output_path}")
    print()

    # Wheel sources, in search order.
    sources: list[tuple[str, list[Path]]] = [
        ("dist", sorted(DIST_DIR.glob("*.whl")) if DIST_DIR.is_dir() else [])
    ]
    for raw in args.whl_dir:
        whl_dir = Path(raw).expanduser().resolve()
        if not whl_dir.is_dir():
            fail(f"--whl-dir folder not found: {whl_dir}")
        sources.append((f"whl-dir {whl_dir}", sorted(whl_dir.glob("*.whl"))))
    if args.fetch_external:
        if not ext_sources:
            print("NOTE: --fetch-external given, but no [[tool.nxp-mcp.external]] "
                  "entries are declared in pyproject.toml.")
        for src in ext_sources:
            try:
                paths = fetch_external(src, manifest)
            except (RuntimeError, OSError) as exc:
                fail(str(exc))
            sources.append((f"{src['source']} {src['label']}", paths))
        print()

    # Collect all required WHL files
    whl_map = collect_whl_files(manifest, sources, externals, root_pins(pyproject, manifest))
    print(f"Found {len(whl_map)} wheel files:")
    for name, (path, origin) in sorted(whl_map.items()):
        print(f"  {path.name}  ({format_size(path.stat().st_size)})  [{origin}]")
    print()

    # Check installer source
    for entry in manifest.INSTALLER_ENTRIES:
        entry_path = INSTALLER_DIR / entry
        if not entry_path.exists():
            print(f"ERROR: Installer source not found: {entry_path}")
            sys.exit(1)

    # Build the .pyz archive
    if output_path.exists():
        output_path.unlink()

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:

        # 1. Installer entry point and manifest module(s).
        for entry in manifest.INSTALLER_ENTRIES:
            entry_path = INSTALLER_DIR / entry
            if entry.startswith("_manifest"):
                print(f"Adding installer manifest ({entry})...")
                zf.writestr(entry, build_manifest_bytes(version, entry_path))
            else:
                print(f"Adding installer entry point ({entry})...")
                zf.write(entry_path, entry)

        # 2. Wheel files
        print(f"Adding {len(whl_map)} wheel files...")
        for name, (whl_path, _origin) in sorted(whl_map.items()):
            arcname = f"{manifest.WHL_DIR}/{whl_path.name}"
            zf.write(whl_path, arcname)
            print(f"  + {arcname}")
        print()

        # 3. Component assets (files and folders)
        print("Adding component assets...")
        asset_count = add_component_assets(zf, manifest)
        print(f"  Added {asset_count} asset files")
        print()

    # Make the .pyz self-executable (prepend shebang)
    _prepend_shebang(output_path)

    final_size = output_path.stat().st_size
    print()
    print("=" * 60)
    print("  Package created successfully!")
    print(f"  File:    {output_path}")
    print(f"  Size:    {format_size(final_size)}")
    print(f"  Variant: {args.variant}")
    print("=" * 60)
    print()
    print("  To install on Windows:")
    print(f"    python {output_path.name}")
    print()
    print("  To install to a custom directory:")
    print(f"    python {output_path.name} --dest C:\\NXP\\mcp-core")
    print()
    print("  Silent install (no prompts):")
    print(f"    python {output_path.name} --dest C:\\NXP\\mcp-core --yes")
    print()


def _prepend_shebang(pyz_path: Path) -> None:
    """Prepend a Python shebang to make the .pyz directly executable on Unix."""
    shebang = b"#!/usr/bin/env python3\n"
    original = pyz_path.read_bytes()
    if not original.startswith(b"#!"):
        pyz_path.write_bytes(shebang + original)


if __name__ == "__main__":
    main()
