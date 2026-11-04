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

"""NXP Agentic AI Toolkit - GitHub release update checker (manifest-driven).

Compares the NXP MCP server packages installed locally against a GitHub
release and reports, per package, what is up to date, outdated or missing.
Can also list releases, download assets and install an update.

Every release attaches a small JSON manifest, ``release-manifest.json``, that
declares what the release ships, what each package requires (NXP and
third-party), where the NXP dependencies come from, and which content archives
(configs / skills / kb) are attached. The manifest is mandatory: a release
without it is not updater-compatible. See scripts/MANIFEST_SPEC.md.

Sub-commands
------------
    check       (default) Report local vs release, per package, and whether the
                release is compatible with this environment. No download.
    list        List releases (or raw tags with --tags), with a compat column.
    download    Download release assets (wheels, content archives or the
                manifest). No install.
    update      check -> plan -> download -> install (wheels via pip), then
                apply the content archives.
    rollback    Restore content from a previous update's backup.

Examples
--------
    # --repo is required: pass the GitHub repository as OWNER/NAME or a full URL.

    # Check installed MCP packages against the latest release (default command)
    python github_update_toolkit.py --repo owner/name
    python github_update_toolkit.py --repo owner/name --tag v1.0.0 check

    # List published releases (or raw git tags)
    python github_update_toolkit.py --repo owner/name list
    python github_update_toolkit.py --repo owner/name list --tags --limit 20

    # Download the release wheels (default) or the manifest / a content archive
    python github_update_toolkit.py --repo owner/name download
    python github_update_toolkit.py --repo owner/name download --kind manifest
    python github_update_toolkit.py --repo owner/name download --kind skills -p freemaster

    # Update one tool against its own release repo (wheels -> current env)
    python github_update_toolkit.py --repo nxp-agentic-ai/freemaster-ai-support update

    # Update but also pull the NXP dependencies that block the install
    python github_update_toolkit.py --repo nxp-agentic-ai/freemaster-ai-support update --with-deps

    # Only update one kind of content, or skip some kinds
    python github_update_toolkit.py --repo owner/name update --only skills
    python github_update_toolkit.py --repo owner/name update --skip kb

    # Non-interactive update (no prompts). Run it with the target environment's
    #   Python so the wheels install into that very venv, e.g.:
    C:/NXP/MCP/.venv/Scripts/python.exe github_update_toolkit.py --repo owner/name update --yes

    # Private repo / higher rate limits: provide a GitHub token
    #   (or set GITHUB_TOKEN / GH_TOKEN in the environment)
    python github_update_toolkit.py --repo owner/name --token <TOKEN> check
"""


import argparse
import filecmp
import hashlib
import http.client
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


# --- Constants & paths -------------------------------------------------------

GITHUB_API = "https://api.github.com"
USER_AGENT = "nxp-mcp-update-toolkit"

# setuptools entry-point group every MCP server package registers itself under.
SERVER_ENTRY_POINT_GROUP = "nxp.mcp.servers"

# The release manifest asset and the schema version this updater understands.
MANIFEST_ASSET_NAME = "release-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MAX_BYTES = 1024 * 1024      # refuse to load larger manifests
COMPAT_SCAN_LIMIT = 20                # releases scanned for "latest compatible"

# Content-archive kind -> default subfolder under the target root.
CONTENT_DIRS = {"configs": "configs", "skills": "skills", "kb": "knowledge_db"}

# The kinds a single update may touch (selected via --only / --skip).
UPDATE_KINDS = ("packages", "configs", "skills", "kb")


class UpdaterError(Exception):
    """A manifest / integrity / selection failure. main() maps it to exit 1."""


# --- Small helpers -----------------------------------------------------------

def normalize(name: str) -> str:
    """Normalise a distribution name: lower-case, collapse [-_.]+ to '-'."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _split_packages(value: str) -> list[str]:
    """argparse type: split a comma-separated --packages value into tokens."""
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_version(text: str) -> tuple:
    """Parse a version into a comparable tuple, tolerating a leading 'v'.

    Only the leading dotted-number run is compared, e.g. "v1.2.3-rc1" -> (1,2,3).
    Missing/non-numeric input degrades to (0,). Pre/post/dev/local segments are
    ignored, so "2.0.0rc1" compares equal to "2.0.0".
    """
    if not text:
        return (0,)
    match = re.match(r"(\d+(?:\.\d+)*)", str(text).strip().lstrip("vV"))
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(local: str | None, remote: str | None) -> int:
    """Return -1 if local < remote, 0 if equal, 1 if local > remote.

    A missing local version counts as older; a missing remote as newer-than-local.
    """
    if remote is None:
        return 1 if local else 0
    if local is None:
        return -1
    lv, rv = parse_version(local), parse_version(remote)
    length = max(len(lv), len(rv))
    lv += (0,) * (length - len(lv))
    rv += (0,) * (length - len(rv))
    return (lv > rv) - (lv < rv)


def short_name(dist_name: str) -> str | None:
    """Map a distribution name to the installer's --packages short name.

    "nxp-mcp-compiler" -> "compiler". The meta-package "nxp-mcp" and the
    always-installed "nxp-mcp-shared" have no selectable short name (None).
    """
    norm = normalize(dist_name)
    if norm in ("nxp-mcp", "nxp-mcp-shared"):
        return None
    if norm.startswith("nxp-mcp-"):
        return norm[len("nxp-mcp-"):]
    return None


# --- Version specifiers ------------------------------------------------------

def _prefix_match(version: str, prefix: str) -> bool:
    """Return True when the numeric tuple of *version* starts with *prefix*."""
    pv = parse_version(version)
    wv = parse_version(prefix)
    n = len(wv)
    pv = pv + (0,) * (n - len(pv))
    return pv[:n] == wv


def parse_spec(spec: str) -> list[tuple[str, str]]:
    """Split a PEP 440 specifier into (op, version) clauses.

    An unparsable clause is returned as ("", clause) so callers can treat it as
    satisfied. An empty spec yields an empty list.
    """
    clauses: list[tuple[str, str]] = []
    for raw in str(spec or "").split(","):
        clause = raw.strip()
        if not clause:
            continue
        m = re.match(r"(===|==|!=|~=|>=|<=|>|<)\s*(.+)", clause)
        if not m:
            clauses.append(("", clause))
            continue
        clauses.append((m.group(1), m.group(2).strip()))
    return clauses


def _clause_ok(version: str, op: str, want: str) -> bool:
    """Return True when *version* satisfies a single (op, want) clause.

    Handles the cases the old code got wrong: ``==X.*`` / ``!=X.*`` prefix
    matching, and the full ``~=`` compatible-release semantics (floor plus a
    prefix match on all but the last component).
    """
    if op == "===":
        return version == want
    if op in ("==", "!=") and want.endswith(".*"):
        matched = _prefix_match(version, want[:-2])
        return matched if op == "==" else not matched
    if op == "~=":
        base = parse_version(want)
        floor_ok = compare_versions(version, want) >= 0
        if len(base) < 2:
            return floor_ok
        prefix = ".".join(str(x) for x in base[:-1])
        return floor_ok and _prefix_match(version, prefix)
    cmp = compare_versions(version, want)
    return {
        "==": cmp == 0,
        "!=": cmp != 0,
        ">=": cmp >= 0,
        "<=": cmp <= 0,
        ">": cmp > 0,
        "<": cmp < 0,
    }.get(op, True)


def version_satisfies(version: str, spec: str) -> bool:
    """Return True when *version* satisfies the comma-separated *spec*.

    An empty spec is always satisfied. A clause that cannot be parsed is treated
    as satisfied, so unknown syntax never invents a false conflict.
    """
    if not str(spec or "").strip():
        return True
    for op, want in parse_spec(spec):
        if op == "":
            continue
        if not _clause_ok(version, op, want):
            return False
    return True


def spec_pins_exact(spec: str) -> str | None:
    """Return v when *spec* is exactly one "==v" clause with no wildcard."""
    clauses = parse_spec(spec)
    if len(clauses) == 1:
        op, want = clauses[0]
        if op == "==" and not want.endswith(".*"):
            return want
    return None


# --- Environment markers -----------------------------------------------------

_MARKER_VARS = {
    "os_name": lambda: os.name,
    "sys_platform": lambda: sys.platform,
    "platform_system": lambda: platform.system(),
    "platform_machine": lambda: platform.machine(),
    "python_version": lambda: ".".join(platform.python_version_tuple()[:2]),
    "python_full_version": lambda: platform.python_version(),
    "implementation_name": lambda: sys.implementation.name,
}
_VERSION_VARS = {"python_version", "python_full_version"}


class _MarkerError(Exception):
    pass


def _tokenize_marker(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(("lparen", "("))
            i += 1
            continue
        if c == ")":
            tokens.append(("rparen", ")"))
            i += 1
            continue
        if c in "'\"":
            j = text.find(c, i + 1)
            if j == -1:
                raise _MarkerError("unterminated string literal")
            tokens.append(("str", text[i + 1:j]))
            i = j + 1
            continue
        two = text[i:i + 2]
        if two in ("==", "!=", "<=", ">="):
            tokens.append(("op", two))
            i += 2
            continue
        if c in "<>":
            tokens.append(("op", c))
            i += 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", text[i:])
        if not m:
            raise _MarkerError(f"unexpected character {c!r}")
        word = m.group(0)
        i += len(word)
        if word in ("and", "or"):
            tokens.append(("bool", word))
        elif word == "in":
            tokens.append(("op", "in"))
        elif word == "not":
            tokens.append(("not", "not"))
        else:
            tokens.append(("var", word))
    return tokens


class _MarkerParser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ("", "")

    def parse_or(self) -> bool:
        left = self.parse_and()
        while self.peek() == ("bool", "or"):
            self.pos += 1
            right = self.parse_and()
            left = left or right
        return left

    def parse_and(self) -> bool:
        left = self.parse_atom()
        while self.peek() == ("bool", "and"):
            self.pos += 1
            right = self.parse_atom()
            left = left and right
        return left

    def parse_atom(self) -> bool:
        if self.peek()[0] == "lparen":
            self.pos += 1
            val = self.parse_or()
            if self.peek()[0] != "rparen":
                raise _MarkerError("missing closing parenthesis")
            self.pos += 1
            return val
        return self.parse_comparison()

    def parse_comparison(self) -> bool:
        left = self.parse_operand()
        tok = self.peek()
        if tok[0] == "op":
            op = tok[1]
            self.pos += 1
        elif tok[0] == "not":
            self.pos += 1
            if self.peek() == ("op", "in"):
                self.pos += 1
                op = "not in"
            else:
                raise _MarkerError("expected 'in' after 'not'")
        else:
            raise _MarkerError("expected a comparison operator")
        right = self.parse_operand()
        return _apply_marker_op(left, op, right)

    def parse_operand(self) -> tuple[str, str]:
        tok = self.peek()
        if tok[0] in ("var", "str"):
            self.pos += 1
            return tok
        raise _MarkerError("expected an operand")


def _marker_value(operand: tuple[str, str]) -> tuple[str, bool]:
    kind, val = operand
    if kind == "str":
        return val, False
    if val not in _MARKER_VARS:
        raise _MarkerError(f"unknown marker variable {val!r}")
    return _MARKER_VARS[val](), (val in _VERSION_VARS)


def _apply_marker_op(left: tuple[str, str], op: str,
                     right: tuple[str, str]) -> bool:
    lval, lver = _marker_value(left)
    rval, rver = _marker_value(right)
    if op == "in":
        return lval in rval
    if op == "not in":
        return lval not in rval
    if lver or rver:
        c = compare_versions(lval, rval)
        return {"==": c == 0, "!=": c != 0, "<": c < 0, "<=": c <= 0,
                ">": c > 0, ">=": c >= 0}[op]
    return {"==": lval == rval, "!=": lval != rval, "<": lval < rval,
            "<=": lval <= rval, ">": lval > rval, ">=": lval >= rval}[op]


def evaluate_marker(marker: str) -> bool | None:
    """Evaluate a PEP 508 environment marker against the running interpreter.

    Supports and / or / parentheses and comparisons in both orders. Returns
    True/False, or None when the marker cannot be evaluated (a parse error or
    an unknown variable such as "extra"). A None result means "cannot evaluate;
    leave it to pip".
    """
    text = str(marker or "").strip()
    if not text:
        return True
    try:
        tokens = _tokenize_marker(text)
        parser = _MarkerParser(tokens)
        result = parser.parse_or()
        if parser.pos != len(tokens):
            return None
        return bool(result)
    except Exception:
        return None


def _marker_applies(marker: str | None) -> bool | None:
    """Tri-state: True (no marker or evaluates true), False, or None (unknown)."""
    if not marker:
        return True
    return evaluate_marker(marker)


# --- Configuration -----------------------------------------------------------

def resolve_repo(cli_repo: str) -> str:
    """Normalise the --repo value to "owner/name".

    Accepts "owner/name" or a full GitHub URL and normalises to "owner/name".
    """
    candidate = cli_repo.strip()
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", candidate)
    if match:
        return match.group(1)
    return candidate


def resolve_token(cli_token: str | None) -> str | None:
    """Return a GitHub token from --token or the environment, if any."""
    return cli_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


# --- Local package discovery -------------------------------------------------

def installed_servers() -> dict[str, dict]:
    """Return installed MCP servers keyed by normalised distribution name.

    Scans the "nxp.mcp.servers" entry-point group in the *current* interpreter.
    Each value is {"short": <entry-point name>, "version": <dist version>}.
    """
    result: dict[str, dict] = {}
    try:
        eps = importlib.metadata.entry_points(group=SERVER_ENTRY_POINT_GROUP)
    except TypeError:
        # Older importlib.metadata returns a group-keyed mapping.
        eps = importlib.metadata.entry_points().get(SERVER_ENTRY_POINT_GROUP, [])
    for ep in eps:
        dist = getattr(ep, "dist", None)
        version = getattr(dist, "version", "") if dist else ""
        name = ""
        if dist is not None and getattr(dist, "metadata", None) is not None:
            name = (dist.metadata["Name"] or "").strip()
        key = normalize(name) if name else f"?{ep.name}"
        result[key] = {"short": ep.name, "version": version}
    return result


def local_version() -> str | None:
    """Return the local toolkit version from installed nxp-mcp* distributions.

    Prefers the "nxp-mcp" meta-package; otherwise the highest version among any
    installed "nxp-mcp-*" distribution.
    """
    versions: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = (dist.metadata["Name"] or "").strip()
        if normalize(name).startswith("nxp-mcp"):
            versions[normalize(name)] = dist.version
    if not versions:
        return None
    if versions.get("nxp-mcp"):
        return versions["nxp-mcp"]
    return max((v for v in versions.values() if v), key=parse_version, default=None)


def _parse_requirement(req: str) -> tuple[str, str, str] | None:
    """Parse a ``Requires-Dist`` string into (name, specifier, marker).

    Accepts the common PEP 508 shapes, e.g. ``nxp-mcp-shared==1.0.0``,
    ``nxp-mcp-shared (>=1.0,<2.0)`` and ``requests>=2 ; extra == "foo"``. The
    name is normalised; the specifier is the raw version constraint text
    (possibly empty) with any wrapping parentheses stripped; the marker is the
    text after ';' (possibly empty). Returns None when the name cannot be read.
    """
    text = str(req).strip()
    if not text:
        return None
    marker = ""
    if ";" in text:
        text, _, marker = text.partition(";")
        text = text.strip()
        marker = marker.strip()
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    if not match:
        return None
    name = match.group(1)
    rest = text[match.end():].strip()
    # Drop an optional extras group such as "[foo,bar]".
    if rest.startswith("["):
        close = rest.find("]")
        if close != -1:
            rest = rest[close + 1:].strip()
    specifier = rest.strip()
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1].strip()
    return normalize(name), specifier, marker


def installed_distributions() -> dict[str, dict]:
    """Return every installed distribution with its applicable pins.

    Covers ALL installed distributions, not only nxp-mcp*, because third-party
    versions are needed by the compatibility engine too. Each value is
    ``{"version": <ver>, "pins": [(dep_norm, spec), ...]}``. A pin is kept only
    when its marker is absent or evaluates True; ``extra`` requirements, markers
    that evaluate False and markers that cannot be evaluated are all excluded.
    """
    result: dict[str, dict] = {}
    for dist in importlib.metadata.distributions():
        name = (dist.metadata["Name"] or "").strip()
        norm = normalize(name)
        if not norm:
            continue
        pins: list[tuple[str, str]] = []
        for raw in (dist.requires or []):
            parsed = _parse_requirement(raw)
            if parsed is None:
                continue
            dep_norm, specifier, marker = parsed
            if marker and "extra" in marker:
                continue
            if _marker_applies(marker) is not True:
                continue
            pins.append((dep_norm, specifier))
        result[norm] = {"version": dist.version, "pins": pins}
    return result


# --- GitHub REST helpers -----------------------------------------------------

def _request(url: str, token: str | None, accept: str) -> urllib.request.Request:
    """Build a GitHub request with the standard headers."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _api_get(url: str, token: str | None) -> object:
    """GET a GitHub API URL and return parsed JSON, or raise RuntimeError."""
    req = _request(url, token, "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = f" - {json.loads(exc.read().decode('utf-8')).get('message', '')}"
        except Exception:
            pass
        if exc.code == 404:
            raise RuntimeError(
                f"GitHub returned 404 for {url}. Check the repo name and, for "
                f"private repos, that GITHUB_TOKEN is set.{detail}"
            ) from exc
        if exc.code in (401, 403):
            raise RuntimeError(
                f"GitHub returned {exc.code} for {url}. Usually an auth or "
                f"rate-limit issue; set GITHUB_TOKEN.{detail}"
            ) from exc
        raise RuntimeError(f"GitHub API error {exc.code} for {url}.{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error contacting GitHub: {exc.reason}") from exc


def latest_release(repo: str, token: str | None) -> dict | None:
    """Return the latest release JSON, falling back to the newest of all."""
    try:
        return _api_get(f"{GITHUB_API}/repos/{repo}/releases/latest", token)
    except RuntimeError:
        releases = list_releases(repo, token)
        return releases[0] if releases else None


def list_releases(repo: str, token: str | None) -> list:
    """Return all releases (most recent first)."""
    data = _api_get(f"{GITHUB_API}/repos/{repo}/releases?per_page=100", token)
    return data if isinstance(data, list) else []


def list_tags(repo: str, token: str | None) -> list:
    """Return all tags."""
    data = _api_get(f"{GITHUB_API}/repos/{repo}/tags?per_page=100", token)
    return data if isinstance(data, list) else []


def release_by_tag(repo: str, tag: str, token: str | None) -> dict | None:
    """Return a specific release by tag name, or None."""
    try:
        return _api_get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", token)
    except RuntimeError:
        return None


def iter_candidate_releases(repo: str, token: str | None, limit: int) -> list:
    """Return up to *limit* published releases, newest first.

    Drafts and prereleases are skipped so the compatibility scan only considers
    releases a user could actually install.
    """
    out = []
    for rel in list_releases(repo, token):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        out.append(rel)
        if limit and len(out) >= limit:
            break
    return out


# --- Manifest access ---------------------------------------------------------

def asset_by_name(release: dict, name: str) -> dict | None:
    """Return the release asset whose filename equals *name*, or None."""
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")) == name:
            return asset
    return None


def _missing_manifest_msg(tag: str | None, repo: str) -> str:
    """The exact message for a release that ships no manifest asset."""
    return (
        f"==> Release {tag} of {repo} has no {MANIFEST_ASSET_NAME}\n"
        f"    asset, so it is not updater-compatible. Pick another release (see 'list')\n"
        f"    or download its assets manually from the GitHub release page."
    )


def _download_bytes(url: str, token: str | None, limit: int) -> bytes:
    """Download at most *limit* bytes from *url*; error if the limit is passed.

    Kept separate from download_asset() so tests can mock it to serve a
    manifest without a real HTTP round-trip.
    """
    req = _request(url, token, "application/octet-stream")
    opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect)
    try:
        with opener.open(req, timeout=60) as resp:
            data = resp.read(limit + 1)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise RuntimeError(f"Failed to download manifest: {exc}") from exc
    if len(data) > limit:
        raise UpdaterError(f"manifest at {url} exceeds {limit} bytes; refusing")
    return data


def fetch_manifest(repo: str, release: dict, token: str | None,
                   cache: dict) -> dict:
    """Fetch, parse and validate a release's manifest; cache by (repo, tag).

    Raises UpdaterError when the asset is missing, the JSON is bad, or the
    manifest fails validation. Prints any validation warnings. The validated
    manifest is normalised in place and gets a private ``_release`` back-link.
    """
    tag = release.get("tag_name")
    key = (normalize(repo), tag)
    if key in cache:
        return cache[key]
    asset = asset_by_name(release, MANIFEST_ASSET_NAME)
    if asset is None:
        raise UpdaterError(_missing_manifest_msg(tag, repo))
    url = asset.get("url") or asset.get("browser_download_url")
    # Transient status while the manifest downloads: the CDN connect can stall
    # for many seconds. Shown only on a terminal and erased afterwards, so
    # piped output, logs and tests are unchanged.
    show = sys.stdout.isatty()
    status = ""
    if show:
        width = shutil.get_terminal_size().columns
        status = f"Fetching manifest {repo} {tag}..."[:max(width - 1, 0)]
        print(status, end="", flush=True)
    try:
        data = _download_bytes(url, token, MANIFEST_MAX_BYTES)
    finally:
        if show:
            print("\r" + " " * len(status) + "\r", end="", flush=True)

    try:
        manifest = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdaterError(f"manifest of {tag} is not valid JSON: {exc}") from exc
    warnings = validate_manifest(manifest, release, repo)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)
    manifest["_release"] = release
    cache[key] = manifest
    return manifest


def validate_manifest(manifest: dict, release: dict | None,
                      repo: str | None) -> list[str]:
    """Apply the schema rules, normalise in place, return warnings.

    Normalisation: package and dependency names are normalised; each
    ``requires`` value becomes ``{"spec": str, "marker": str | None}``; a short
    content ``package`` becomes the full package name. Errors raise
    UpdaterError.
    """
    errors: list[str] = []
    warnings: list[str] = []

    sv = manifest.get("schema_version")
    if sv != MANIFEST_SCHEMA_VERSION:
        if isinstance(sv, int) and sv > MANIFEST_SCHEMA_VERSION:
            raise UpdaterError(
                f"manifest schema {sv} is newer than this updater supports; "
                f"update the updater")
        raise UpdaterError(
            f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}, "
            f"got {sv!r}")

    if not isinstance(manifest.get("repo"), str):
        errors.append("repo must be a string")
    elif repo and normalize(manifest["repo"]) != normalize(repo):
        warnings.append(f"manifest repo {manifest['repo']} differs from "
                        f"--repo {repo}")
    if not isinstance(manifest.get("version"), str):
        errors.append("version must be a string")

    packages = manifest.get("packages")
    if not isinstance(packages, list):
        raise UpdaterError("manifest packages must be a list")
    # A content-only release ships no wheels (packages == []); that is valid.
    # The wheels step is simply skipped when there is nothing to install.

    names: set[str] = set()
    assets: set[str] = set()
    pkg_versions: dict[str, str] = {}
    for pkg in packages:
        if not isinstance(pkg, dict):
            raise UpdaterError("every packages[] entry must be an object")
        pkg["name"] = normalize(pkg.get("name", ""))
        name = pkg["name"]
        for field in ("version", "asset", "sha256"):
            if not isinstance(pkg.get(field), str) or not pkg.get(field):
                errors.append(f"packages[{name}].{field} must be a non-empty string")
        if not isinstance(pkg.get("size"), int):
            errors.append(f"packages[{name}].size must be an int")
        if name in names:
            errors.append(f"duplicate package name {name}")
        names.add(name)
        pkg_versions[name] = pkg.get("version", "")
        asset = pkg.get("asset", "")
        if asset in assets:
            errors.append(f"duplicate asset name {asset}")
        assets.add(asset)
        req = pkg.get("requires") or {}
        if not isinstance(req, dict):
            errors.append(f"packages[{name}].requires must be an object")
            req = {}
        norm_req: dict[str, dict] = {}
        for dep, val in req.items():
            dnorm = normalize(dep)
            if isinstance(val, dict):
                norm_req[dnorm] = {"spec": val.get("spec", "") or "",
                                   "marker": val.get("marker") or None}
            else:
                norm_req[dnorm] = {"spec": val or "", "marker": None}
        pkg["requires"] = norm_req

    deps = manifest.get("dependencies")
    if deps is None:
        deps = {}
    if not isinstance(deps, dict):
        errors.append("dependencies must be an object")
        deps = {}
    norm_deps: dict[str, dict] = {}
    for dep, val in deps.items():
        dnorm = normalize(dep)
        if isinstance(val, dict):
            entry: dict = {"spec": val.get("spec", "") or ""}
            if val.get("repo"):
                entry["repo"] = val["repo"]
            if val.get("marker"):
                entry["marker"] = val["marker"]
        else:
            entry = {"spec": val or ""}
        norm_deps[dnorm] = entry
        if dnorm.startswith("nxp-mcp") and not entry.get("repo"):
            warnings.append(f"dependency {dnorm} starts with nxp-mcp but has no "
                            f"repo; treated as an index dependency")
    manifest["dependencies"] = norm_deps

    # Intra-release pins and dependency coverage.
    for pkg in packages:
        name = pkg["name"]
        for dep, req in pkg["requires"].items():
            if dep in pkg_versions:
                if not version_satisfies(pkg_versions[dep], req["spec"]):
                    errors.append(
                        f"{name} requires {dep}{req['spec']}, but this release "
                        f"ships {dep} {pkg_versions[dep]}")
            elif dep not in norm_deps:
                errors.append(
                    f"{name} requires {dep} which is neither a package of this "
                    f"release nor listed in dependencies")

    content = manifest.get("content") or {}
    if content and not isinstance(content, dict):
        errors.append("content must be an object")
        content = {}
    cfg = content.get("configs")
    if cfg is not None and not isinstance(cfg, dict):
        errors.append("content.configs must be an object")
    for kind in ("skills", "kb"):
        items = content.get(kind) or []
        if items and not isinstance(items, list):
            errors.append(f"content.{kind} must be a list")
            continue
        seen_units: set[str] = set()
        for item in items:
            units = item.get("units")
            if not isinstance(units, list) or not units:
                errors.append(f"content.{kind} item has empty units")
                units = []
            pkgname = normalize(item.get("package", ""))
            # A content-only release ships no wheels, so there are no release
            # package names to match against; keep the (normalised) name as is.
            if names and pkgname not in names:
                short = "nxp-mcp-" + pkgname
                if short in names:
                    pkgname = short
                else:
                    errors.append(f"content.{kind} package {pkgname} is not one "
                                  f"of the release packages")
            item["package"] = pkgname
            for u in units:
                if u in seen_units:
                    errors.append(f"content.{kind} unit {u} appears in two "
                                  f"archives")
                seen_units.add(u)

    # Asset presence on the release.
    if release is not None:
        rel_assets = {str(a.get("name", "")) for a in (release.get("assets") or [])}
        wanted = set(assets)
        if cfg and isinstance(cfg, dict) and cfg.get("asset"):
            wanted.add(cfg["asset"])
        for kind in ("skills", "kb"):
            for item in content.get(kind) or []:
                if item.get("asset"):
                    wanted.add(item["asset"])
        for a in wanted:
            if a and a not in rel_assets:
                errors.append(f"asset {a} listed in the manifest is missing from "
                              f"the release")

    if errors:
        raise UpdaterError("manifest is invalid:\n  - " + "\n  - ".join(errors))
    return warnings


def select_packages(manifest: dict, tokens: list[str]) -> list[dict]:
    """Return the manifest packages selected by *tokens* plus their closure.

    No tokens selects every package. A token matches as given or with the
    ``nxp-mcp-`` prefix. The result is extended with the intra-release closure
    (same-release packages required by selected ones, recursively). An unknown
    token raises UpdaterError listing the available names.
    """
    by_name = {p["name"]: p for p in manifest["packages"]}
    if not tokens:
        return list(manifest["packages"])
    wanted: list[str] = []
    for tok in tokens:
        norm = normalize(tok)
        if norm in by_name:
            wanted.append(norm)
        elif ("nxp-mcp-" + norm) in by_name:
            wanted.append("nxp-mcp-" + norm)
        else:
            raise UpdaterError(
                f"unknown package {tok!r}; available: "
                f"{', '.join(sorted(by_name))}")
    chosen: dict[str, None] = {}

    def add(name: str) -> None:
        if name in chosen:
            return
        chosen[name] = None
        for dep in by_name[name]["requires"]:
            if dep in by_name:
                add(dep)

    for name in wanted:
        add(name)
    return [p for p in manifest["packages"] if p["name"] in chosen]


def resolve_nxp_dependency(dep: str, spec: str, dep_repo: str,
                           token: str | None, cache: dict):
    """Return the newest release of *dep_repo* providing *dep* that fits *spec*.

    Returns (release, manifest, package) or None. Releases without a manifest or
    with an invalid one are skipped silently.
    """
    for rel in iter_candidate_releases(dep_repo, token, COMPAT_SCAN_LIMIT):
        if asset_by_name(rel, MANIFEST_ASSET_NAME) is None:
            continue
        try:
            man = fetch_manifest(dep_repo, rel, token, cache)
        except UpdaterError:
            continue
        for pkg in man["packages"]:
            if pkg["name"] == dep and version_satisfies(pkg["version"], spec):
                return rel, man, pkg
    return None


# --- Compatibility engine ----------------------------------------------------

def evaluate_release(manifest: dict, selected: list[dict], env: dict, *,
                     with_deps: bool, repo: str, token: str | None,
                     cache: dict) -> dict:
    """Decide whether installing *selected* from *manifest* fits *env*.

    Returns a dict with ``blockers``, ``conflicts``, ``warnings``, ``planned``
    (name -> {version, source, asset, sha256, size, release, manifest}) and
    ``index_changes``. A release is compatible when it has no blockers and no
    conflicts. No download happens here beyond manifest fetches during NXP
    dependency resolution.
    """
    blockers: list[dict] = []
    conflicts: list[dict] = []
    warnings: list[str] = []
    index_changes: list[dict] = []
    planned: dict[str, dict] = {}

    release = manifest.get("_release")

    # 1. Python.
    py = manifest.get("python")
    if py and not version_satisfies(platform.python_version(), py):
        blockers.append({"kind": "python", "required": py,
                         "running": platform.python_version()})

    # 2. Planned set seeded from the selected packages of this release.
    for p in selected:
        planned[p["name"]] = {
            "version": p["version"], "source": "this release",
            "asset": p.get("asset"), "sha256": p.get("sha256"),
            "size": p.get("size"), "release": release, "manifest": manifest,
        }

    release_names = {p["name"] for p in manifest["packages"]}

    def forward(pkg: dict, man: dict, names_here: set[str],
                out_fetch: list) -> None:
        for dep, req in (pkg.get("requires") or {}).items():
            spec = req["spec"]
            if _marker_applies(req.get("marker")) is not True:
                continue
            if dep in names_here or dep in planned:
                continue
            installed = env.get(dep)
            if installed and version_satisfies(installed["version"], spec):
                continue
            source = (man.get("dependencies") or {}).get(dep, {}).get("repo")
            if source:
                if installed is None or with_deps:
                    out_fetch.append((dep, spec, source, pkg["name"]))
                else:
                    blockers.append({
                        "kind": "nxp_dep", "dep": dep, "spec": spec,
                        "installed": installed["version"], "source": source,
                        "requirer": pkg["name"],
                    })
            else:
                if installed is None:
                    index_changes.append({"dep": dep, "installed": None,
                                          "spec": spec})
                else:
                    index_changes.append({"dep": dep,
                                          "installed": installed["version"],
                                          "spec": spec})

    # 3. Forward check over the selected packages.
    queue: list = []
    for p in selected:
        forward(p, manifest, release_names, queue)

    # 4. Resolve NXP dependencies that must be fetched, recursively (depth<=3).
    visited: set[str] = set()
    depth = 0
    while queue and depth < 3:
        nxt: list = []
        for dep, spec, source, _requirer in queue:
            if dep in planned or dep in visited:
                continue
            visited.add(dep)
            res = resolve_nxp_dependency(dep, spec, source, token, cache)
            if res is None:
                blockers.append({"kind": "nxp_unresolvable", "dep": dep,
                                 "spec": spec, "source": source})
                continue
            rel, man, pkg = res
            planned[pkg["name"]] = {
                "version": pkg["version"],
                "source": f"{source} {rel.get('tag_name')}",
                "asset": pkg.get("asset"), "sha256": pkg.get("sha256"),
                "size": pkg.get("size"), "release": rel, "manifest": man,
            }
            forward(pkg, man, {q["name"] for q in man["packages"]}, nxt)
        queue = nxt
        depth += 1

    # 5. Reverse check: installed tools (not being replaced) whose pins break.
    index_map = {ic["dep"]: ic["spec"] for ic in index_changes}
    for tool, info in env.items():
        if tool in planned:
            continue
        for dep, spec_t in info["pins"]:
            if not spec_t:
                continue
            if dep in planned:
                v = planned[dep]["version"]
                if not version_satisfies(v, spec_t):
                    conflicts.append({"tool": tool,
                                      "tool_version": info["version"],
                                      "dep": dep, "required": spec_t,
                                      "planned": v})
            elif dep in index_map:
                new_spec = index_map[dep]
                s_exact = spec_pins_exact(new_spec)
                t_exact = spec_pins_exact(spec_t)
                if s_exact is not None:
                    if not version_satisfies(s_exact, spec_t):
                        conflicts.append({"tool": tool,
                                          "tool_version": info["version"],
                                          "dep": dep, "required": spec_t,
                                          "new_spec": new_spec})
                elif t_exact is not None:
                    if not version_satisfies(t_exact, new_spec):
                        conflicts.append({"tool": tool,
                                          "tool_version": info["version"],
                                          "dep": dep, "required": spec_t,
                                          "new_spec": new_spec})
                else:
                    warnings.append(f"possible conflict: {tool} requires "
                                    f"{dep}{spec_t}, update sets {dep}{new_spec}")

    return {"blockers": blockers, "conflicts": conflicts, "warnings": warnings,
            "planned": planned, "index_changes": index_changes}


def find_latest_compatible(repo: str, token: str | None, cache: dict,
                           env: dict, tokens: list[str],
                           exclude_tag: str | None):
    """Return the newest (release, manifest) that evaluates with no blockers.

    Uses the default compatibility definition (with_deps=False). Releases
    without a manifest or with an invalid one are skipped silently.
    """
    for rel in iter_candidate_releases(repo, token, COMPAT_SCAN_LIMIT):
        if rel.get("tag_name") == exclude_tag:
            continue
        if asset_by_name(rel, MANIFEST_ASSET_NAME) is None:
            continue
        try:
            man = fetch_manifest(repo, rel, token, cache)
            selected = select_packages(man, tokens)
        except UpdaterError:
            continue
        ev = evaluate_release(man, selected, env, with_deps=False, repo=repo,
                              token=token, cache=cache)
        if not ev["blockers"] and not ev["conflicts"]:
            return rel, man
    return None


def _blocker_reason(blockers: list[dict]) -> str:
    """Return a short one-line reason for the first blocker."""
    if not blockers:
        return ""
    b = blockers[0]
    if b["kind"] == "python":
        return f"requires Python {b['required']}, running {b['running']}"
    if b["kind"] == "nxp_dep":
        return f"needs {b['dep']}{b['spec']}, installed {b['installed']}"
    if b["kind"] == "nxp_unresolvable":
        return f"no release provides {b['dep']} {b['spec']}"
    return "incompatible"


def _conflict_tools(conflicts: list[dict]) -> str:
    """Return a comma list of the tools a conflict set would break."""
    seen: dict[str, None] = {}
    for c in conflicts:
        seen.setdefault(c["tool"], None)
    return ", ".join(seen.keys())


def _print_dependency_conflicts(conflicts: list[dict]) -> None:
    """Print a per-tool warning for an install that would break pins."""
    print("\n" + "=" * 72, file=sys.stderr)
    print("WARNING: dependency conflicts detected", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for c in conflicts:
        brings = c.get("planned") or c.get("new_spec")
        print(f"  - {c['tool']} {c['tool_version']} requires "
              f"{c['dep']}{c['required']}, but the update brings "
              f"{c['dep']} {brings}", file=sys.stderr)
    print("\nThese tools may fail to import after the update. Also update them, "
          "or pin the dependency.", file=sys.stderr)


# --- Content archives (configs / skills / knowledge DB) ----------------------

def _zip_members(zip_path: Path) -> list[str]:
    """Return the file members of a zip (directories excluded)."""
    with zipfile.ZipFile(zip_path) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def extract_zip_flat(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract every file of *zip_path* into *dest_dir*, returning written paths.

    The archive is expected to hold its files flat (no wrapper folder); any
    internal subpaths are preserved beneath dest_dir. Returns the list of
    extracted file paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            target = dest_dir / member
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(target)
    return written


def _overlay_units(zip_path: Path, unit: str) -> list[str]:
    """Return the per-component subtrees an overlay archive replaces.

    The archives keep their parent folders, so one zip may carry several
    components. *unit* decides the replacement granularity, matched against each
    member's path (POSIX separators):
      - "topdir": the first path segment, e.g. "freemaster" for a skills zip
        laid out as "freemaster/SKILL.md".
      - "lance":  the path up to and including the first "*.lance" segment, e.g.
        "bge-base-en-v1.5/freemaster.lance" for a kb zip. A model folder that
        carries several tables yields one unit per table, so sibling tables and
        other models are never touched.
      - "file":   every file member is its own unit, e.g. "gateway.stdio.yaml"
        for the flat configs zip. Only the files the archive carries are
        replaced; any other file already in the target folder is left alone.
    Returns the distinct subtree-relative paths, first-seen order preserved.
    """
    units: dict[str, None] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            parts = [p for p in member.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            if unit == "file":
                if member.endswith("/"):
                    continue  # directory entry - files carry their own path
                rel = "/".join(parts)
            elif unit == "lance":
                lance_idx = next(
                    (i for i, seg in enumerate(parts) if seg.endswith(".lance")),
                    None,
                )
                if lance_idx is None:
                    continue
                rel = "/".join(parts[:lance_idx + 1])
            else:  # "topdir"
                if member.endswith("/") and len(parts) == 1:
                    rel = parts[0]
                elif len(parts) < 2 and not member.endswith("/"):
                    # A loose top-level file (no component folder) - skip; these
                    # archives are expected to nest under a component directory.
                    continue
                else:
                    rel = parts[0]
            units.setdefault(rel, None)
    return list(units.keys())


def apply_overlay_archive(zip_path: Path, dest_parent: Path, *,
                          unit: str, do_backup: bool,
                          backup_root: Path | None = None,
                          manifest: dict | None = None,
                          kind: str = "",
                          skip_identical: bool = False) -> None:
    """Overlay an archive into *dest_parent* per component subtree, atomically.

    Unlike a full-directory replace, this only replaces the subtrees the archive
    actually carries (a skills component folder, a single "*.lance" table, or a
    single config file), leaving sibling components / tables / models / files
    in *dest_parent* intact - which is what the repo-split model needs (a tool
    release ships only its own skill, its own kb table and its own configs).

    With *skip_identical* a file unit whose content already matches the live
    copy byte for byte is left untouched: it is neither backed up nor replaced,
    so the backup only ever holds files the update really changed.

    The apply is staged: the archive is first extracted into a temporary folder
    next to *dest_parent*, each existing subtree is moved OUT of the live tree
    into ``backup_root/<kind>/<rel>`` (so a "*.bak" never lingers inside skills/
    or knowledge_db/ where a scanner could mistake it for real content), and
    only then is the staged subtree moved into place. If anything fails midway
    the already-placed subtrees are removed and the backed-up ones are moved
    back, leaving the live tree as it was. When *do_backup* is False (or
    *backup_root* is None) the existing subtree is simply removed instead of
    backed up. Backed-up subtrees are recorded in *manifest* for ``rollback``.
    """
    dest_parent.mkdir(parents=True, exist_ok=True)
    units = _overlay_units(zip_path, unit)

    # Stage inside dest_parent so the final moves stay on one filesystem.
    staging = Path(tempfile.mkdtemp(prefix=".overlay-", dir=dest_parent))
    moved_backups: list[tuple[Path, Path]] = []  # (original_target, backup_path)
    placed: list[Path] = []                       # targets moved into place
    added: list[str] = []                         # units that did not exist yet
    replaced: list[str] = []                      # units that overwrote a live copy
    unchanged: list[str] = []                     # identical units left untouched
    try:
        extract_zip_flat(zip_path, staging)

        for rel in units:
            target = dest_parent / rel
            staged = staging / rel
            if not staged.exists():
                continue
            if (skip_identical and target.is_file() and staged.is_file()
                    and filecmp.cmp(str(target), str(staged), shallow=False)):
                # Same bytes as the live copy: nothing to back up or replace.
                staged.unlink()
                unchanged.append(rel)
                continue
            if target.exists():
                replaced.append(rel)
                if do_backup and backup_root is not None:
                    bdest = backup_root / (kind or "overlay") / rel
                    bdest.parent.mkdir(parents=True, exist_ok=True)
                    if bdest.exists():
                        shutil.rmtree(bdest) if bdest.is_dir() else bdest.unlink()
                    shutil.move(str(target), str(bdest))
                    moved_backups.append((target, bdest))
                    if manifest is not None:
                        manifest.setdefault("subtrees", []).append({
                            "kind": kind,
                            "rel": rel,
                            "backup": str(bdest),
                            "dest": str(target),
                        })
                    print(f"  backed up {rel} -> {bdest}")
                else:
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
            else:
                added.append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(target))
            placed.append(target)

        # Sweep any remaining loose files the archive carried (not part of a
        # recognised unit) into place - matches the prior blanket extract.
        for item in [p for p in staging.rglob("*") if p.is_file()]:
            rel = item.relative_to(staging)
            dst = dest_parent / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.is_file():
                dst.unlink()
            if not dst.exists():
                shutil.move(str(item), str(dst))

        noun = "file" if unit == "file" else "subtree"
        summary = f"{len(added)} new, {len(replaced)} replaced"
        if skip_identical:
            summary += f", {len(unchanged)} unchanged"
        print(f"  applied {len(placed)} {noun}(s) -> {dest_parent}  ({summary})")
        for rel in added:
            print(f"    + {rel}")
        for rel in replaced:
            print(f"    ~ {rel}")
    except Exception as exc:  # noqa: BLE001 - restore then re-raise
        print(f"  ERROR applying overlay ({exc}); rolling back...",
              file=sys.stderr)
        for target in placed:
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
        restored = {str(b) for _, b in moved_backups}
        for original, bdest in moved_backups:
            if bdest.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(bdest), str(original))
        if manifest is not None:
            # Drop the records for subtrees we just rolled back so a later
            # rollback command does not point at backups that no longer exist.
            manifest["subtrees"] = [
                s for s in manifest.get("subtrees", [])
                if s.get("backup") not in restored
            ]
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _format_size(size_bytes: float) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# --- Downloads ---------------------------------------------------------------

_BAR_WIDTH = 30            # characters inside the [###---] progress bar
_CHUNK_SIZE = 64 * 1024    # upper bound for a single read1() call
_REDRAW_INTERVAL = 0.1     # minimum seconds between two progress-bar redraws
_MIN_RATE_WINDOW = 0.2     # do not show a rate measured over a shorter time


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that drops ``Authorization`` when the host changes.

    GitHub answers an asset download with a 302 to a pre-signed URL on its
    download CDN; the signature in that URL is the credential there. The stock
    handler copies every request header onto the redirected request, which
    would also send the GitHub token to the CDN host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(new_req.full_url).hostname
            if old_host != new_host:
                new_req.remove_header("Authorization")
        return new_req


def _progress_line(downloaded: int, total: int, elapsed: float, *,
                   width: int = 0, took: float | None = None) -> str:
    """Return one progress line for *downloaded* of *total* bytes.

    *elapsed* is the time spent receiving the body and gives the rate, which
    is left out until it is measured over a meaningful window. *took* (final
    line only) is the wall time including connect and redirect. With an
    unknown *total* (0) only the byte count is shown.

    A positive *width* limits the line to ``width - 1`` columns: a line that
    wraps in the terminal can no longer be redrawn in place with a carriage
    return. The optional fields (total time, then rate) are dropped whole
    first; the text is only cut when even the bar and byte count do not fit.
    """
    parts = []
    if total:
        frac = min(downloaded / total, 1.0)
        filled = int(_BAR_WIDTH * frac)
        parts.append(f"[{'#' * filled}{'-' * (_BAR_WIDTH - filled)}] {frac * 100:3.0f}%")
        parts.append(f"{_format_size(downloaded)}/{_format_size(total)}")
    else:
        parts.append(f"{_format_size(downloaded)} received")
    optional = []
    if downloaded and elapsed >= _MIN_RATE_WINDOW:
        optional.append(f"{_format_size(downloaded / elapsed)}/s")
    if took is not None:
        optional.append(f"in {took:.1f} s")
    limit = width - 1 if width > 0 else 0
    text = "      " + "  ".join(parts + optional)
    while limit and len(text) > limit and optional:
        optional.pop()
        text = "      " + "  ".join(parts + optional)
    return text[:limit] if limit else text


def download_asset(asset: dict, dest_dir: Path, token: str | None) -> Path:
    """Download a release asset into dest_dir and return the local path.

    Uses the asset's ``url`` (the API endpoint) with an
    ``application/octet-stream`` Accept header so it works for private repos
    when a token is supplied; ``browser_download_url`` is a public fallback.
    The token is not forwarded when GitHub redirects to its download CDN.

    The data goes to ``<name>.part`` and is renamed to ``<name>`` only after
    the size check passes (API size, else Content-Length), so an aborted or
    truncated download never leaves a half-written file under the real name.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = asset.get("url") or asset.get("browser_download_url")
    # Keep only the final path component: the name becomes part of a path.
    name = Path(str(asset.get("name") or "")).name
    if not name:
        public = str(asset.get("browser_download_url") or "")
        name = Path(urllib.parse.urlsplit(public).path).name or "download.bin"
    if not url:
        raise RuntimeError(f"Asset {name!r} has no downloadable URL.")

    expected = int(asset.get("size", 0) or 0)
    target = dest_dir / name
    part = target.with_name(target.name + ".part")

    size_txt = f" ({_format_size(expected)})" if expected else ""
    print(f"      Downloading {name}{size_txt}...", flush=True)

    interactive = sys.stdout.isatty()
    width = shutil.get_terminal_size().columns if interactive else 0
    shown = 0  # length of the progress text currently on the terminal line

    def draw(text: str) -> None:
        # Overwrite the current line; pad so a shorter text hides the old one.
        nonlocal shown
        print("\r" + text.ljust(shown), end="", flush=True)
        shown = len(text)

    req = _request(url, token, "application/octet-stream")
    opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect)
    total = expected
    downloaded = 0
    finished = False
    t_start = t_body = time.monotonic()
    try:
        if interactive:
            draw("      connecting...")
        try:
            with opener.open(req, timeout=60) as resp, open(part, "wb") as fh:
                if not total:
                    try:
                        total = int(resp.headers.get("Content-Length") or 0)
                    except ValueError:
                        total = 0
                read = getattr(resp, "read1", resp.read)
                t_body = last = time.monotonic()
                if interactive:
                    draw(_progress_line(0, total, 0.0, width=width))
                while True:
                    chunk = read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if interactive and now - last >= _REDRAW_INTERVAL:
                        draw(_progress_line(downloaded, total, now - t_body,
                                            width=width))
                        last = now
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise RuntimeError(f"Failed to download {name}: {exc}") from exc

        now = time.monotonic()
        line = _progress_line(downloaded, total, now - t_body, width=width,
                              took=now - t_start)
        if interactive:
            draw(line)
            print()
            shown = 0
        else:
            print(line)

        if total and downloaded != total:
            raise RuntimeError(
                f"Size mismatch for {name}: expected {total} bytes, "
                f"got {downloaded} bytes. The download may be corrupt."
            )
        try:
            os.replace(part, target)
        except OSError as exc:  # e.g. target locked by another process
            raise RuntimeError(f"Could not save {target}: {exc}") from exc
        finished = True
    finally:
        if shown:
            print(flush=True)  # do not leave the cursor on a half-drawn bar
        if not finished:
            try:
                part.unlink()
            except OSError:
                pass
    return target


def verify_sha256(path: Path, expected: str) -> None:
    """Verify the sha256 of *path*; on mismatch delete it and raise.

    An empty *expected* skips the check. On success nothing is returned.
    """
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got.lower() != str(expected).lower():
        try:
            path.unlink()
        except OSError:
            pass
        raise UpdaterError(
            f"sha256 mismatch for {path.name}: expected {expected}, got {got}")


def detect_install_root() -> Path | None:
    """Return the toolkit install root: the folder above this script's dir.

    The updater lives at ``<install-root>/scripts/github_update_toolkit.py``,
    so the install root is the script's grandparent. This is where configs/,
    skills/ and knowledge_db/ live, independent of where the active venv sits.
    """
    return Path(__file__).resolve().parent.parent


def _missing_dists_from_pip_output(text: str) -> list[str]:
    """Scan pip output for dependency requirements it could not resolve.

    Matches pip's two common phrasings and returns the raw requirement tokens
    (e.g. "nxp-mcp-shared==1.0.0"), de-duplicated in first-seen order.
    """
    if not text:
        return []
    pattern = re.compile(
        r"(?:Could not find a version that satisfies the requirement|"
        r"No matching distribution found for)\s+(\S+)"
    )
    seen: dict[str, None] = {}
    for match in pattern.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen.keys())


def pip_install(dist_paths: list[Path],
                find_links: Path | None = None) -> tuple[int, list[str]]:
    """Install the given wheels into the CURRENT interpreter.

    Uses ``sys.executable -m pip install --upgrade`` so the running Python's
    environment is the install target. ``find_links`` (the download directory)
    lets intra-repo pins such as ``nxp-mcp-shared==X`` resolve against sibling
    wheels downloaded alongside; third-party dependencies resolve from the
    configured index.

    Returns ``(exit_code, unresolved)`` where *unresolved* is the list of
    requirement tokens pip could not find (parsed from its output).
    """
    if not dist_paths:
        print("Nothing to install.", file=sys.stderr)
        return 1, []
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if find_links is not None:
        cmd += ["--find-links", str(find_links)]
    cmd += [str(p) for p in dist_paths]
    print(f"\nRunning: {' '.join(cmd)}\n")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True)
    except OSError as exc:
        print(f"Failed to run pip: {exc}", file=sys.stderr)
        return 1, []
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    missing = _missing_dists_from_pip_output(f"{proc.stdout}\n{proc.stderr}")
    return proc.returncode, missing


def pip_check() -> list[str]:
    """Run ``python -m pip check`` and return any unsatisfied-requirement lines.

    Returns the lines (empty when the environment is consistent or pip check
    could not be run).
    """
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "check"],
                              text=True, capture_output=True)
    except OSError as exc:
        print(f"  (pip check could not run: {exc})", file=sys.stderr)
        return []
    if proc.returncode == 0:
        return []
    out = (proc.stdout or "") + (proc.stderr or "")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _print_pip_missing(missing: list[str]) -> None:
    """Print the generic 'pip could not find X' remediation hint."""
    req = ", ".join(missing)
    print(
        f"\n==> Install failed: pip could not find '{req}'.\n"
        f"    If it is an NXP package, the release manifest must list it under\n"
        f"    \"dependencies\" with its GitHub \"repo\"; otherwise check your "
        f"package index.",
        file=sys.stderr,
    )


# --- Reporting ---------------------------------------------------------------

def build_rows(installed: dict[str, dict],
               remote_pkgs: dict[str, str]) -> list[dict]:
    """Build the per-package comparison rows.

    *remote_pkgs* maps a normalised distribution name to the version published
    in the release (from the manifest packages). A row exists for every
    installed server and for every remote server package. Status is one of
    ok / outdated / ahead / external / not-installed.
    """
    # Drop infrastructure-only distributions (nxp-mcp meta, nxp-mcp-shared) that
    # have no server short name; installed servers are always kept.
    remote_pkgs = {n: v for n, v in remote_pkgs.items() if short_name(n) is not None}
    remote_set = set(remote_pkgs)
    all_names = list(dict.fromkeys(list(installed.keys()) + list(remote_pkgs)))

    rows: list[dict] = []
    for name in all_names:
        info = installed.get(name)
        local_v = info["version"] if info else ""
        in_remote = name in remote_set
        remote_v = remote_pkgs.get(name, "").lstrip("vV") if in_remote else ""
        short = (info["short"] if info else short_name(name)) or "-"
        if info and not in_remote:
            status = "external"
        elif in_remote and not info:
            status = "not-installed"
        else:
            cmp = compare_versions(local_v or None, remote_v or None)
            status = "ok" if cmp == 0 else ("outdated" if cmp < 0 else "ahead")
        rows.append({
            "short": short,
            "package": name,
            "local": local_v or "-",
            "remote": remote_v or "-",
            "status": status,
        })

    status_order = {
        "outdated": 0,
        "not-installed": 1,
        "ahead": 2,
        "ok": 3,
        "external": 4,
    }
    rows.sort(key=lambda r: (status_order.get(r["status"], 99), r["short"]))
    return rows


def print_table(rows: list[dict]) -> None:
    """Print the per-package rows as an aligned table."""
    headers = {"short": "server", "package": "package",
               "local": "local", "remote": "remote", "status": "status"}
    cols = ["short", "package", "local", "remote", "status"]
    widths = {c: max(len(headers[c]), *(len(r[c]) for r in rows)) if rows
              else len(headers[c]) for c in cols}
    line = "  ".join(headers[c].ljust(widths[c]) for c in cols)
    print(line)
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def update_verdict(selected: list[dict], env: dict) -> list[dict]:
    """Return the selected packages that are outdated or not installed."""
    pending: list[dict] = []
    for p in selected:
        local = env.get(p["name"], {}).get("version")
        if local is None or compare_versions(local, p["version"]) < 0:
            pending.append(p)
    return pending


# --- Selection helpers -------------------------------------------------------

def _kinds_type(value: str) -> list[str]:
    """argparse type for --only / --skip: validate against UPDATE_KINDS."""
    out = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        if part not in UPDATE_KINDS:
            raise argparse.ArgumentTypeError(
                f"invalid kind {part!r}; choose from {', '.join(UPDATE_KINDS)}")
        out.append(part)
    return out


def _resolve_kinds(args) -> set[str]:
    """Compute the active kinds from --only / --skip."""
    only = set(getattr(args, "only", None) or [])
    skip = set(getattr(args, "skip", None) or [])
    base = only if only else set(UPDATE_KINDS)
    return base - skip


def _token_matches_package(package: str, tokens: list[str]) -> bool:
    """Return True when *package* matches one of *tokens* (with nxp-mcp- form)."""
    for tok in tokens:
        norm = normalize(tok)
        if package == norm or package == f"nxp-mcp-{norm}":
            return True
    return False


def _filter_items_by_packages(items: list[dict], tokens: list[str]) -> list[dict]:
    """Keep content items whose package matches one of *tokens* (all if empty)."""
    if not tokens:
        return list(items)
    return [it for it in items if _token_matches_package(it["package"], tokens)]


# --- Backup root & manifest --------------------------------------------------

BACKUPS_DIRNAME = "backups"
BACKUP_MANIFEST_NAME = "backup-manifest.json"


def _backups_root(target_root: Path) -> Path:
    """Return the dedicated backups root, OUTSIDE the live content trees."""
    return target_root / BACKUPS_DIRNAME


def _new_backup_dir(target_root: Path) -> Path:
    """Create and return a fresh ``backups/update-<timestamp>`` directory."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = _backups_root(target_root) / f"update-{stamp}"
    candidate = base
    n = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{n}")
        n += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _write_backup_manifest(backup_dir: Path, manifest: dict) -> None:
    """Write (or remove) the per-update backup manifest.

    If nothing was backed up, the empty backup directory is removed so the
    backups root stays tidy.
    """
    has_content = bool(manifest.get("subtrees"))
    if not has_content:
        try:
            if backup_dir.exists() and not any(backup_dir.iterdir()):
                backup_dir.rmdir()
                parent = backup_dir.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
        except OSError:
            pass
        return
    (backup_dir / BACKUP_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  backup manifest: {backup_dir / BACKUP_MANIFEST_NAME}")


def _prune_backups(target_root: Path, keep: int) -> None:
    """Keep only the newest *keep* ``update-*`` backup folders; prune the rest.

    A keep value <= 0 disables pruning (keep everything).
    """
    if keep is None or keep <= 0:
        return
    root = _backups_root(target_root)
    if not root.is_dir():
        return
    backups = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("update-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        print(f"  pruned old backup: {old.name}")


def _resolve_target_root(args) -> Path:
    """Where content archives (configs/skills/kb) get extracted."""
    if getattr(args, "target_root", None):
        return Path(args.target_root).resolve()
    return detect_install_root() or Path.cwd()


def _prompt_dir(label: str, default: Path, assume_yes: bool) -> Path:
    """Ask the user where to extract an archive; Enter accepts *default*."""
    if assume_yes:
        return default
    raw = input(f"  {label} target [{default}]: ").strip()
    return Path(raw).resolve() if raw else default


def _ask_yes_no(question: str, *, default: bool, assume_yes: bool) -> bool:
    """Prompt a yes/no question; *assume_yes* short-circuits to True."""
    if assume_yes:
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _content_default_dir(args, target_root: Path, kind: str) -> Path:
    """Return the extraction directory for a content *kind*, honouring overrides."""
    override = getattr(args, f"{'kb' if kind == 'kb' else kind}_dir", None)
    if kind == "configs":
        override = getattr(args, "configs_dir", None)
    elif kind == "skills":
        override = getattr(args, "skills_dir", None)
    elif kind == "kb":
        override = getattr(args, "kb_dir", None)
    if override:
        return Path(override).resolve()
    return target_root / CONTENT_DIRS[kind]


def _warn_unit_diff(zip_path: Path, unit: str, expected: list[str] | None) -> None:
    """Warn when the archive's actual overlay units differ from the manifest."""
    if expected is None:
        return
    actual = _overlay_units(zip_path, unit)
    if set(actual) != set(expected):
        print(f"  WARNING: {zip_path.name} units {sorted(actual)} differ from "
              f"manifest units {sorted(expected)}", file=sys.stderr)


def _select_content_items(items: list[dict], selected_names: set[str] | None,
                          present_names: set[str]):
    """Partition content items into (kept, skipped) by package membership.

    An item is kept when its package is present (installed or planned) and,
    when a package filter is active, also selected. *skipped* holds
    (item, reason) pairs.
    """
    kept: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for item in items:
        pkg = item["package"]
        if selected_names is not None and pkg not in selected_names:
            skipped.append((item, "not selected"))
            continue
        if pkg not in present_names:
            skipped.append((item, "package not installed"))
            continue
        kept.append(item)
    return kept, skipped


# --- Sub-commands ------------------------------------------------------------

def cmd_check(args, repo: str, token: str | None) -> int:
    """Report local vs release, per package, plus the compatibility verdict."""
    cache: dict = {}
    release = (release_by_tag(repo, args.tag, token) if args.tag
               else latest_release(repo, token))
    local_v = local_version()
    print(f"Repo:              {repo}")
    print(f"Local version:     {local_v or '(none installed)'}")
    label = "Release:" if args.tag else "Latest release:"
    if release is None:
        print(f"{label:<19}(no release found)")
        if args.tag:
            print(f"\n==> Release {args.tag} not found on GitHub.")
        else:
            print("\n==> No release found on GitHub.")
        return 1    

    tag = release.get("tag_name")
    manifest = fetch_manifest(repo, release, token, cache)
    env = installed_distributions()
    selected = select_packages(manifest, [])
    ev = evaluate_release(manifest, selected, env, with_deps=False, repo=repo,
                          token=token, cache=cache)
    compatible = not ev["blockers"] and not ev["conflicts"]
    if compatible:
        print(f"{label:<19}{tag} (compatible)")        
    else:
        reason = (_blocker_reason(ev["blockers"]) if ev["blockers"]
                  else "conflicts: " + _conflict_tools(ev["conflicts"]))
        print(f"{label:<19}{tag} (incompatible: {reason})")
        lc = find_latest_compatible(repo, token, cache, env, [], exclude_tag=tag)
        if lc:
            lc_tag = lc[0].get("tag_name")
            note = (" (installed)" if local_v
                    and compare_versions(local_v, lc[1].get("version")) == 0
                    else "")
            print(f"Latest compatible: {lc_tag}{note}")
        else:
            print(f"Latest compatible: none found in the last "
                  f"{COMPAT_SCAN_LIMIT} releases")
    
    content = manifest.get("content") or {}
    n_pkgs = len(manifest.get("packages") or [])
    n_skills = len(content.get("skills") or [])
    n_kb = len(content.get("kb") or [])
    has_cfg = bool(content.get("configs"))
    print(f"Content:           packages ({n_pkgs}), skills ({n_skills}), "
          f"kb ({n_kb}), configs ({'yes' if has_cfg else 'no'})")            
    print()

    installed = installed_servers()
    remote_pkgs = {p["name"]: p["version"] for p in manifest["packages"]}
    if not installed:
        print("No MCP servers found in this Python (nxp.mcp.servers is empty).")
    else:
        print_table(build_rows(installed, remote_pkgs))
    print()

    pending = update_verdict(selected, env)
    if pending:
        what = ", ".join(short_name(p["name"]) or p["name"] for p in pending)
        print(f"==> Update available in {tag}: {what}")
        print("    Run 'github_update_toolkit.py update' to download and install it.")
    elif n_pkgs == 0 and (n_skills or n_kb or has_cfg):
        print(f"==> {tag} is a content-only release (no packages).")
        print("    Run 'github_update_toolkit.py update --only skills,kb,configs' "
              "to apply it.")
    else:
        print("==> You are up to date.")

    return 0


def _release_compat_label(repo: str, rel: dict, token: str | None,
                          cache: dict, env: dict) -> str:
    """Return the compat column value for one release in `list`."""
    if asset_by_name(rel, MANIFEST_ASSET_NAME) is None:
        return "no manifest"
    try:
        man = fetch_manifest(repo, rel, token, cache)
        selected = select_packages(man, [])
    except UpdaterError:
        return "invalid manifest"
    ev = evaluate_release(man, selected, env, with_deps=False, repo=repo,
                          token=token, cache=cache)
    if ev["blockers"]:
        return "incompatible: " + _blocker_reason(ev["blockers"])
    if ev["conflicts"]:
        return "conflicts: " + _conflict_tools(ev["conflicts"])
    return "compatible"


def cmd_list(args, repo: str, token: str | None) -> int:
    """List releases (default) or raw tags (--tags) published on GitHub."""
    if args.tags:
        tags = list_tags(repo, token)
        if not tags:
            print("No tags found.")
            return 0
        for t in tags:
            print(t.get("name", "?"))
        return 0

    limit = getattr(args, "limit", 10)
    releases = list_releases(repo, token)
    if not releases:
        print("No releases found.")
        return 0
    if limit and limit > 0:
        releases = releases[:limit]
    cache: dict = {}
    env = installed_distributions()
    print(f"{'tag':<16} {'published':<22} {'compat':<34} name")
    for rel in releases:
        tag = rel.get("tag_name", "?")
        name = rel.get("name") or ""
        published = rel.get("published_at") or ""
        flags = []
        if rel.get("prerelease"):
            flags.append("prerelease")
        if rel.get("draft"):
            flags.append("draft")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        compat = _release_compat_label(repo, rel, token, cache, env)
        print(f"{tag:<16} {published:<22} {compat:<34} {name}{suffix}")
    return 0


def cmd_download(args, repo: str, token: str | None) -> list[Path]:
    """Download release assets of the requested kind (no install)."""
    kind = getattr(args, "kind", "wheel") or "wheel"
    release = (release_by_tag(repo, args.tag, token) if args.tag
               else latest_release(repo, token))
    if not release:
        print("No release found to download from.", file=sys.stderr)
        return []
    tag = release.get("tag_name")
    dest_dir = Path(args.dest).resolve() if args.dest else (Path.cwd() / "dist")
    tokens = getattr(args, "packages", None) or []

    if kind == "manifest":
        asset = asset_by_name(release, MANIFEST_ASSET_NAME)
        if asset is None:
            raise UpdaterError(_missing_manifest_msg(tag, repo))
        p = download_asset(asset, dest_dir, token)
        print(f"Saved: {p}")
        return [p]

    cache: dict = {}
    manifest = fetch_manifest(repo, release, token, cache)

    if kind == "wheel":
        selected = select_packages(manifest, tokens)
        paths: list[Path] = []
        for pkg in selected:
            asset = asset_by_name(release, pkg["asset"])
            if asset is None:
                raise UpdaterError(f"asset {pkg['asset']} missing from {tag}")
            p = download_asset(asset, dest_dir, token)
            verify_sha256(p, pkg["sha256"])
            paths.append(p)
        for p in paths:
            print(f"Saved: {p}")
        return paths

    content = manifest.get("content") or {}
    if kind == "configs":
        cfg = content.get("configs")
        if not cfg:
            print("configs: not shipped in this release.", file=sys.stderr)
            return []
        asset = asset_by_name(release, cfg["asset"])
        p = download_asset(asset, dest_dir, token)
        verify_sha256(p, cfg["sha256"])
        print(f"Saved: {p}")
        return [p]

    if kind in ("skills", "kb"):
        items = _filter_items_by_packages(content.get(kind) or [], tokens)
        if not items:
            print(f"{kind}: nothing to download.", file=sys.stderr)
            return []
        paths = []
        for item in items:
            asset = asset_by_name(release, item["asset"])
            p = download_asset(asset, dest_dir, token)
            verify_sha256(p, item["sha256"])
            paths.append(p)
        for p in paths:
            print(f"Saved: {p}")
        return paths
    return []


def _load_backups(target_root: Path) -> list[tuple[Path, dict]]:
    """Return (backup_dir, manifest) pairs, newest first, that have a manifest."""
    root = _backups_root(target_root)
    if not root.is_dir():
        return []
    found: list[tuple[Path, dict]] = []
    for d in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        mf = d / BACKUP_MANIFEST_NAME
        if d.is_dir() and mf.is_file():
            try:
                found.append((d, json.loads(mf.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError):
                continue
    return found


def cmd_rollback(args, repo: str, token: str | None) -> int:
    """Restore content from a previous update's backup manifest."""
    target_root = _resolve_target_root(args)
    backups = _load_backups(target_root)

    if getattr(args, "list", False):
        if not backups:
            print(f"No backups found under {_backups_root(target_root)}.")
            return 0
        print(f"Backups under {_backups_root(target_root)} (newest first):")
        for d, mf in backups:
            entries = mf.get("subtrees", [])
            kinds: dict[str, int] = {}
            for e in entries:
                kinds[e.get("kind") or "?"] = kinds.get(e.get("kind") or "?", 0) + 1
            detail = ", ".join(f"{k}={n}" for k, n in kinds.items()) or "none"
            line = f"  {d.name}  tag={mf.get('tag', '?')}  restorable: {detail}"
            n_new = len(mf.get("new_files", []))
            if n_new:
                line += f"  (legacy .new files={n_new})"
            print(line)
        return 0

    if not backups:
        print(f"No backups found under {_backups_root(target_root)}.",
              file=sys.stderr)
        return 1

    chosen: tuple[Path, dict] | None = None
    wanted = getattr(args, "backup", None)
    if wanted:
        for d, mf in backups:
            if d.name == wanted:
                chosen = (d, mf)
                break
        if chosen is None:
            print(f"Backup '{wanted}' not found. Use 'rollback --list' to see "
                  f"the available ones.", file=sys.stderr)
            return 1
    else:
        chosen = backups[0]

    backup_dir, manifest = chosen
    subtrees = manifest.get("subtrees", [])
    if not subtrees:
        print(f"Backup {backup_dir.name} recorded no replaced content; "
              f"nothing to restore.")
        return 0

    print(f"Restoring {len(subtrees)} item(s) from {backup_dir.name} ...")
    for entry in subtrees:
        print(f"    [{entry.get('kind') or '?'}] {entry.get('rel', entry.get('dest'))}")
    assume_yes = bool(getattr(args, "yes", False))
    if not _ask_yes_no("Proceed with rollback?", default=False,
                       assume_yes=assume_yes):
        print("Rollback cancelled.")
        return 0

    restored = 0
    for entry in subtrees:
        backup = Path(entry["backup"])
        dest = Path(entry["dest"])
        if not backup.exists():
            print(f"  WARNING: backup missing for {entry.get('rel', dest)}; "
                  f"skipping.", file=sys.stderr)
            continue
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(dest))
        print(f"  restored {entry.get('rel', dest)} -> {dest}")
        restored += 1

    new_files = manifest.get("new_files", [])
    if new_files:
        print(f"\nNote: this (older) update also produced {len(new_files)} "
              f"'.new' config file(s); these were never applied over your "
              f"edits and are left as-is:")
        for nf in new_files:
            print(f"  {nf.get('new_file')}")

    print(f"\nRolled back {restored} item(s) from {backup_dir.name}.")
    return 0


def process_extras(args, release: dict, manifest: dict, dest_dir: Path,
                   token: str | None, *, kinds: set[str],
                   selected_names: set[str] | None,
                   present_names: set[str]) -> None:
    """Download and apply the content archives named in the manifest.

    *kinds* is the active set of content kinds (configs/skills/kb). Skills and
    kb items are filtered by package: only those whose ``package`` is present
    (installed or planned) and, when -p was given, selected, are applied.
    When ``packages`` is NOT among *kinds* (an --only skills / --only kb run),
    each archive is additionally gated on the installed version of its package
    against the release version.
    """
    target_root = _resolve_target_root(args)
    assume_yes = bool(getattr(args, "yes", False))
    no_backup = bool(getattr(args, "no_backup", False))
    force = bool(getattr(args, "force", False))
    content = manifest.get("content") or {}
    content_kinds = [k for k in ("configs", "skills", "kb") if k in kinds]
    if not content_kinds:
        return

    packages_step = "packages" in kinds
    inst = installed_distributions()
    rel_versions = {p["name"]: p["version"] for p in manifest["packages"]}

    print("\n--- Content archives ---")
    backup_dir = _new_backup_dir(target_root)
    bk: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repo": getattr(args, "_repo", ""),
        "tag": release.get("tag_name", ""),
        "backup_dir": str(backup_dir),
        "subtrees": [],
    }

    def _version_gate(item: dict, kind: str) -> bool:
        """For --only skills/kb runs, decide whether to apply *item*."""
        pkg = item["package"]
        short = short_name(pkg) or pkg
        installed_ver = inst.get(pkg, {}).get("version")
        relver = rel_versions.get(pkg)
        if installed_ver is None:
            print(f"  {kind} {short}: {pkg} is not installed; skipped")
            return False
        if relver is not None and compare_versions(installed_ver, relver) != 0:
            print(f"  {kind} {short}: release {release.get('tag_name')} ships "
                  f"{pkg} {relver}, installed {installed_ver}")
            if assume_yes:
                if not force:
                    print(f"      skipped (pass --force to apply anyway)")
                    return False
                return True
            return _ask_yes_no("Apply anyway?", default=False, assume_yes=False)
        return True

    # --- configs ------------------------------------------------------------
    if "configs" in kinds:
        cfg = content.get("configs")
        if not cfg:
            print("  configs: not shipped in this release.")
        elif _ask_yes_no("Update configs? (files that differ are replaced; your "
                         "versions are moved to backups/)",
                         default=True, assume_yes=assume_yes):
            dest = _prompt_dir("configs",
                               _content_default_dir(args, target_root, "configs"),
                               assume_yes)
            zp = download_asset(asset_by_name(release, cfg["asset"]), dest_dir, token)
            verify_sha256(zp, cfg["sha256"])
            apply_overlay_archive(zp, dest, unit="file", do_backup=True,
                                  backup_root=backup_dir, manifest=bk,
                                  kind="configs", skip_identical=True)

    # --- skills -------------------------------------------------------------
    if "skills" in kinds:
        items = content.get("skills") or []
        if not items:
            print("  skills: not shipped in this release.")
        else:
            kept, skipped = _select_content_items(items, selected_names,
                                                  present_names)
            for item, reason in skipped:
                short = short_name(item["package"]) or item["package"]
                print(f"  skills {short}: {reason}; skipped")
            if not packages_step:
                kept = [it for it in kept if _version_gate(it, "skills")]
            if kept:
                labels = ", ".join(short_name(i["package"]) or i["package"]
                                   for i in kept)
                if _ask_yes_no(f"Update skills ({len(kept)} archives: {labels})?",
                               default=True, assume_yes=assume_yes):
                    dest = _prompt_dir(
                        "skills",
                        _content_default_dir(args, target_root, "skills"),
                        assume_yes)
                    do_backup = (not no_backup) and _ask_yes_no(
                        "Back up the skill folders being replaced first?",
                        default=True, assume_yes=assume_yes)
                    for item in kept:
                        zp = download_asset(asset_by_name(release, item["asset"]),
                                            dest_dir, token)
                        verify_sha256(zp, item["sha256"])
                        _warn_unit_diff(zp, "topdir", item["units"])
                        apply_overlay_archive(zp, dest, unit="topdir",
                                              do_backup=do_backup,
                                              backup_root=backup_dir,
                                              manifest=bk, kind="skills")

    # --- knowledge DB -------------------------------------------------------
    if "kb" in kinds:
        items = content.get("kb") or []
        if not items:
            print("  kb: not shipped in this release.")
        else:
            kept, skipped = _select_content_items(items, selected_names,
                                                  present_names)
            for item, reason in skipped:
                short = short_name(item["package"]) or item["package"]
                print(f"  kb {short}: {reason}; skipped")
            if not packages_step:
                kept = [it for it in kept if _version_gate(it, "kb")]
            backup_asked = False
            do_backup = False
            for item in kept:
                short = short_name(item["package"]) or item["package"]
                size = item.get("size", 0)
                if assume_yes:
                    go = bool(getattr(args, "with_kb", False))
                    if not go:
                        print(f"  kb {short}: skipped (pass --with-kb to include "
                              f"the knowledge DB in non-interactive runs).")
                else:
                    go = _ask_yes_no(f"Update kb {short} ({_format_size(size)})?",
                                     default=False, assume_yes=False)
                if not go:
                    continue
                if not backup_asked:
                    do_backup = (not no_backup) and _ask_yes_no(
                        "Back up the .lance tables being replaced first?",
                        default=True, assume_yes=assume_yes)
                    backup_asked = True
                dest = _prompt_dir(
                    "knowledge DB",
                    _content_default_dir(args, target_root, "kb"), assume_yes)
                zp = download_asset(asset_by_name(release, item["asset"]),
                                    dest_dir, token)
                verify_sha256(zp, item["sha256"])
                _warn_unit_diff(zp, "lance", item["units"])
                apply_overlay_archive(zp, dest, unit="lance", do_backup=do_backup,
                                      backup_root=backup_dir, manifest=bk,
                                      kind="kb")

    _write_backup_manifest(backup_dir, bk)
    _prune_backups(target_root, getattr(args, "keep_backups", 3))


def _print_incompatible(rtag: str, ev: dict, repo: str, lc,
                        manifest: dict, selected: list[dict], env: dict,
                        token: str | None, cache: dict) -> None:
    """Print the 'cannot be installed' block for a release with blockers."""
    print(f"\n==> {rtag} cannot be installed in this environment:",
          file=sys.stderr)
    for b in ev["blockers"]:
        if b["kind"] == "python":
            print(f"      requires Python {b['required']}, running "
                  f"{b['running']}", file=sys.stderr)
        elif b["kind"] == "nxp_dep":
            print(f"      {b['requirer']} requires {b['dep']}{b['spec']}",
                  file=sys.stderr)
            print(f"      installed: {b['dep']} {b['installed']}",
                  file=sys.stderr)
        elif b["kind"] == "nxp_unresolvable":
            print(f"      no release of {b['source']} provides {b['dep']} "
                  f"{b['spec']}", file=sys.stderr)
    if lc:
        print(f"\n    Latest release compatible with your environment: "
              f"{lc[0].get('tag_name')}", file=sys.stderr)
        print(f"      python github_update_toolkit.py --repo {repo} --tag "
              f"{lc[0].get('tag_name')} update", file=sys.stderr)
    else:
        print(f"\n    No compatible release found in the last "
              f"{COMPAT_SCAN_LIMIT} releases.", file=sys.stderr)
    if ev["blockers"] and all(b["kind"] == "nxp_dep" for b in ev["blockers"]):
        print(f"\n    To get {rtag} you must also update the NXP dependencies:",
              file=sys.stderr)
        print(f"      python github_update_toolkit.py --repo {repo} update "
              f"--with-deps", file=sys.stderr)
        ev2 = evaluate_release(manifest, selected, env, with_deps=True,
                               repo=repo, token=token, cache=cache)
        for c in ev2["conflicts"]:
            print(f"      WARNING: this would break {c['tool']} "
                  f"{c['tool_version']} (requires {c['dep']}{c['required']})",
                  file=sys.stderr)


def _print_plan(repo: str, release: dict, manifest: dict, planned: dict,
                ev: dict | None, kinds: set[str]) -> None:
    """Print the install plan (wheels, index changes and content archives)."""
    print(f"\nPlan for {repo} {release.get('tag_name')}:")
    for name, info in planned.items():
        print(f"  install  {name} {info['version']}   ({info['source']})")
    if ev:
        for ic in ev.get("index_changes", []):
            if ic["installed"] is None:
                print(f"  index    {ic['dep']} {ic['spec']} (from index)")
            else:
                print(f"  index    {ic['dep']} {ic['installed']} -> "
                      f"{ic['spec']} (from index)")
    content = manifest.get("content") or {}
    if "configs" in kinds and content.get("configs"):
        cfg = content["configs"]
        print(f"  configs  {cfg['asset']} ({_format_size(cfg['size'])}) "
              f"per-file merge")
    if "skills" in kinds:
        for item in content.get("skills") or []:
            print(f"  skills   {item['asset']} ({_format_size(item['size'])}) "
                  f"replaces {', '.join(item['units'])}")
    if "kb" in kinds:
        for item in content.get("kb") or []:
            print(f"  kb       {item['asset']} ({_format_size(item['size'])}) "
                  f"replaces {', '.join(item['units'])} (opt-in)")


def cmd_update(args, repo: str, token: str | None) -> int:
    """check -> plan -> download -> install, then apply content archives."""
    cache: dict = {}
    env = installed_distributions()
    tokens = getattr(args, "packages", None) or []
    kinds = _resolve_kinds(args)
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    assume_yes = bool(getattr(args, "yes", False))
    with_deps = bool(getattr(args, "with_deps", False))

    tag = args.tag
    switched = False
    release = manifest = selected = ev = None

    while True:
        release = (release_by_tag(repo, tag, token) if tag
                   else latest_release(repo, token))
        if not release:
            print("No release found to update from.", file=sys.stderr)
            return 1
        rtag = release.get("tag_name")
        manifest = fetch_manifest(repo, release, token, cache)
        selected = select_packages(manifest, tokens)

        local_v = local_version()
        print(f"Repo:            {repo}")
        print(f"Local version:   {local_v or '(none installed)'}")
        print(f"Target release:  {rtag}")
        print()
        installed = installed_servers()
        remote_pkgs = {p["name"]: p["version"] for p in manifest["packages"]}
        if installed:
            print_table(build_rows(installed, remote_pkgs))
            print()

        ev = None
        if "packages" in kinds:
            ev = evaluate_release(manifest, selected, env, with_deps=with_deps,
                                  repo=repo, token=token, cache=cache)
            if ev["blockers"]:
                lc = find_latest_compatible(repo, token, cache, env, tokens,
                                            exclude_tag=rtag)
                _print_incompatible(rtag, ev, repo, lc, manifest, selected, env,
                                    token, cache)
                if assume_yes:
                    if getattr(args, "latest_compatible", False) and lc:
                        tag = lc[0].get("tag_name")
                        switched = True
                        print(f"\n(--latest-compatible: switching to {tag})")
                        continue
                    return 1
                if lc and _ask_yes_no(f"Install {lc[0].get('tag_name')} instead?",
                                      default=False, assume_yes=False):
                    if switched:
                        print("Already switched once; aborting.", file=sys.stderr)
                        return 1
                    tag = lc[0].get("tag_name")
                    switched = True
                    continue
                return 1
            if ev["conflicts"]:
                _print_dependency_conflicts(ev["conflicts"])
                if dry_run:
                    print("\n(dry run: no changes made)")
                    return 1
                if assume_yes:
                    if not force:
                        print("\nAborting (non-interactive and conflicts "
                              "detected). Re-run with --force to install "
                              "anyway, or also update the affected tools.",
                              file=sys.stderr)
                        return 1
                    print("\n(--force: proceeding despite the conflicts above)",
                          file=sys.stderr)
                elif not _ask_yes_no("Proceed anyway?", default=False,
                                     assume_yes=False):
                    print("Aborted. No changes made.")
                    return 1
        break

    content_kinds = [k for k in ("configs", "skills", "kb") if k in kinds]
    pending = update_verdict(selected, env) if "packages" in kinds else []
    if ("packages" in kinds and not pending and not content_kinds
            and not assume_yes and not force):
        answer = input("\nAlready up to date. Reinstall anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing to do.")
            return 0

    if ev is not None:
        planned = ev["planned"]
    else:
        planned = {p["name"]: {"version": p["version"], "source": "this release",
                               "asset": p["asset"], "sha256": p["sha256"],
                               "size": p["size"], "release": release,
                               "manifest": manifest}
                   for p in selected}

    _print_plan(repo, release, manifest, planned, ev, kinds)
    if dry_run:
        print("\n(dry run: no changes made)")
        return 0

    dest_dir = Path(args.dest).resolve() if args.dest else (Path.cwd() / "dist")
    present_names = set(env.keys())
    wheel_paths: list[Path] = []

    if "packages" in kinds:
        for name, info in planned.items():
            rel = info.get("release") or release
            asset = asset_by_name(rel, info["asset"])
            if asset is None:
                raise UpdaterError(f"asset {info['asset']} missing from "
                                   f"{rel.get('tag_name')}")
            p = download_asset(asset, dest_dir, token)
            verify_sha256(p, info["sha256"])
            wheel_paths.append(p)
            present_names.add(name)

        if wheel_paths:
            if not assume_yes:
                answer = input(
                    f"\nInstall {len(wheel_paths)} wheel(s) into this Python "
                    f"now? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Downloaded but not installed.")
                    return 0
            print("(installing into the current environment)")
            rc, missing = pip_install(wheel_paths, find_links=dest_dir)
            if rc != 0 and missing:
                _print_pip_missing(missing)
                return rc
            if rc != 0:
                return rc
            broken = pip_check()
            if broken:
                print("\nWARNING: pip check reports unsatisfied requirements "
                      "after the update:", file=sys.stderr)
                for ln in broken:
                    print(f"  {ln}", file=sys.stderr)
                print("  The affected tools may fail to import; update them too "
                      "or pin the dependency.", file=sys.stderr)

    args._repo = repo
    selected_names = {p["name"] for p in selected} if tokens else None
    process_extras(args, release, manifest, dest_dir, token, kinds=kinds,
                   selected_names=selected_names, present_names=present_names)
    return 0


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github_update_toolkit.py",
        description="Compare locally installed NXP MCP servers against a "
                    "GitHub release, per package.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--repo", metavar="OWNER/NAME", required=True,
                        help="GitHub repository as OWNER/NAME or a full GitHub "
                             "URL (required).")
    parser.add_argument("--token", metavar="TOKEN",
                        help="GitHub token (default: env GITHUB_TOKEN / GH_TOKEN).")
    parser.add_argument("--tag", metavar="vX.Y.Z",
                        help="Target a specific release tag instead of latest.")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="(default) report local vs latest. No download.")

    p_list = sub.add_parser("list", help="list releases (or tags).")
    p_list.add_argument("--tags", action="store_true",
                        help="list raw git tags instead of releases.")
    p_list.add_argument("--limit", metavar="N", type=int, default=10,
                        help="how many releases to list and fetch manifests for "
                             "(0 = all; default: 10).")

    p_dl = sub.add_parser("download", help="download release assets (no install).")
    p_dl.add_argument("--dest", metavar="DIR",
                      help="download directory (default: ./dist).")
    p_dl.add_argument("--kind",
                      choices=("wheel", "configs", "skills", "kb", "manifest"),
                      default="wheel",
                      help="asset kind to download: wheel (default), a content "
                           "archive (configs / skills / kb), or the manifest "
                           "JSON itself.")
    p_dl.add_argument("--packages", "-p", metavar="NAME", type=_split_packages,
                      action="extend", default=[],
                      help="only fetch these packages (comma-separated and/or "
                           "repeatable). Default: all. Also filters skills/kb.")

    p_up = sub.add_parser("update",
                          help="check -> plan -> download -> install.")
    p_up.add_argument("--dest", metavar="DIR",
                      help="download directory for the assets (default: ./dist).")
    p_up.add_argument("--packages", "-p", metavar="NAME", type=_split_packages,
                      action="extend", default=[],
                      help="only install these packages (comma-separated and/or "
                           "repeatable). Selected packages pull their "
                           "intra-release closure. Default: all.")
    p_up.add_argument("--only", metavar="LIST", type=_kinds_type,
                      action="extend", default=[],
                      help="only apply these kinds (comma list of "
                           "packages,configs,skills,kb).")
    p_up.add_argument("--skip", metavar="LIST", type=_kinds_type,
                      action="extend", default=[],
                      help="skip these kinds (comma list of "
                           "packages,configs,skills,kb).")
    p_up.add_argument("--with-deps", action="store_true",
                      help="also fetch and upgrade the NXP dependencies that "
                           "would otherwise block the install.")
    p_up.add_argument("--latest-compatible", action="store_true",
                      help="with --yes, if the target release is incompatible, "
                           "switch automatically to the newest compatible one.")
    p_up.add_argument("--yes", "-y", action="store_true",
                      help="non-interactive: download and install without prompts.")
    p_up.add_argument("--dry-run", action="store_true",
                      help="evaluate and print the plan WITHOUT downloading "
                           "wheels/content or installing anything.")
    p_up.add_argument("--force", action="store_true",
                      help="with --yes, install even when the pre-flight finds a "
                           "dependency conflict (default: abort on conflict).")
    p_up.add_argument("--target-root", metavar="DIR",
                      help="root under which configs/ skills/ knowledge_db/ are "
                           "extracted (default: the install root, else CWD).")
    p_up.add_argument("--configs-dir", metavar="DIR",
                      help="override the configs extraction directory.")
    p_up.add_argument("--skills-dir", metavar="DIR",
                      help="override the skills extraction directory.")
    p_up.add_argument("--kb-dir", metavar="DIR",
                      help="override the knowledge-DB extraction directory.")
    p_up.add_argument("--with-kb", action="store_true",
                      help="include the (large) knowledge-DB archives in "
                           "non-interactive (--yes) runs; interactively you are "
                           "prompted instead.")
    p_up.add_argument("--no-backup", action="store_true",
                      help="replace skills / knowledge_db without taking a "
                           "backup first (configs are always backed up).")
    p_up.add_argument("--keep-backups", metavar="N", type=int, default=3,
                      help="how many timestamped content backups to retain under "
                           "<root>/backups/ (0 keeps all; default: 3).")

    p_rb = sub.add_parser("rollback",
                          help="restore content from a previous update's backup.")
    p_rb.add_argument("--target-root", metavar="DIR",
                      help="root that holds the backups/ folder (default: the "
                           "install root, else CWD).")
    p_rb.add_argument("--backup", metavar="update-YYYYMMDD-HHMMSS",
                      help="restore this specific backup (default: the most "
                           "recent one).")
    p_rb.add_argument("--list", action="store_true",
                      help="list the available backups and exit.")
    p_rb.add_argument("--yes", "-y", action="store_true",
                      help="non-interactive: restore without prompting.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Defaults for attributes not present on every sub-parser.
    for attr, default in (("tags", False), ("dest", None), ("yes", False),
                          ("kind", "wheel"), ("packages", []),
                          ("only", []), ("skip", []), ("with_deps", False),
                          ("latest_compatible", False), ("limit", 10),
                          ("dry_run", False), ("force", False),
                          ("keep_backups", 3), ("backup", None),
                          ("list", False), ("target_root", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    repo = resolve_repo(args.repo)
    token = resolve_token(args.token)

    command = args.command or "check"
    try:
        if command == "check":
            return cmd_check(args, repo, token)
        if command == "list":
            return cmd_list(args, repo, token)
        if command == "download":
            return 0 if cmd_download(args, repo, token) else 1
        if command == "update":
            return cmd_update(args, repo, token)
        if command == "rollback":
            return cmd_rollback(args, repo, token)
    except UpdaterError as exc:
        msg = str(exc)
        print(msg if msg.startswith("==") else f"ERROR: {msg}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
