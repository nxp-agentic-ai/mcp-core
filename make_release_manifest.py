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

"""Generate and validate the NXP MCP release manifest (release-manifest.json).


This generator is standalone and uses the standard library only, so it can be
copied verbatim into every tool repo. It never imports github_update_toolkit.py;
the small helpers it shares (normalize, the spec/marker helpers and the overlay
unit logic) are duplicated here and kept behaviourally identical.

Two modes:

    generate (default)
        Read every wheel in --dist, extract its METADATA, hash the assets and
        auto-detect the content archives, then write release-manifest.json.

    --check
        Load an existing manifest and validate it against --dist (asset
        presence, sha256/size, requires vs the wheel METADATA, dependency
        aggregation and intra-release conflicts).

Examples
--------
    rem Core: no external NXP dependencies
    py -3 scripts\\make_release_manifest.py --repo nxp-agentic-ai/mcp-core

    rem Tool repo with a cross-repo NXP dependency
    py -3 scripts\\make_release_manifest.py --repo nxp-agentic-ai/freemaster-mcp ^
        --dep-repo nxp-mcp-shared=nxp-agentic-ai/mcp-core

    rem After a hand edit of the manifest
    py -3 scripts\\make_release_manifest.py --check
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

MANIFEST_ASSET_NAME = "release-manifest.json"
MANIFEST_SCHEMA_VERSION = 1

GIB = 1024 * 1024 * 1024


# --- Shared helpers (kept identical to github_update_toolkit.py) -------------

def normalize(name: str) -> str:
    """Normalise a distribution name: lower-case, collapse [-_.]+ to '-'."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


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
    """Return -1 if local < remote, 0 if equal, 1 if local > remote."""
    if remote is None:
        return 1 if local else 0
    if local is None:
        return -1
    lv, rv = parse_version(local), parse_version(remote)
    length = max(len(lv), len(rv))
    lv += (0,) * (length - len(lv))
    rv += (0,) * (length - len(rv))
    return (lv > rv) - (lv < rv)


def _prefix_match(version: str, prefix: str) -> bool:
    """Return True when the numeric tuple of *version* starts with *prefix*."""
    pv = parse_version(version)
    wv = parse_version(prefix)
    n = len(wv)
    pv = pv + (0,) * (n - len(pv))
    return pv[:n] == wv


def parse_spec(spec: str) -> list[tuple[str, str]]:
    """Split a PEP 440 specifier into (op, version) clauses.

    An unparsable clause is returned as ("", clause) so callers can decide what
    to do with it. An empty spec yields an empty list.
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
    """Return True when *version* satisfies a single (op, want) clause."""
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


# --- Marker evaluation -------------------------------------------------------

import os as _os
import platform as _platform

_MARKER_VARS = {
    "os_name": lambda: _os.name,
    "sys_platform": lambda: sys.platform,
    "platform_system": lambda: _platform.system(),
    "platform_machine": lambda: _platform.machine(),
    "python_version": lambda: ".".join(_platform.python_version_tuple()[:2]),
    "python_full_version": lambda: _platform.python_version(),
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

    Returns True/False, or None when the marker cannot be evaluated (a parse
    error or an unknown variable such as "extra"). A None result means "leave
    it to pip".
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


def _parse_requirement(req: str) -> tuple[str, str, str] | None:
    """Parse a ``Requires-Dist`` string into (name, specifier, marker)."""
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
    if rest.startswith("["):
        close = rest.find("]")
        if close != -1:
            rest = rest[close + 1:].strip()
    specifier = rest.strip()
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1].strip()
    return normalize(name), specifier, marker


def _overlay_units(zip_path: Path, unit: str) -> list[str]:
    """Return the per-component subtrees an overlay archive replaces.

    Kept identical to github_update_toolkit.py: "topdir" for skills, "lance"
    for kb, "file" for configs.
    """
    units: dict[str, None] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            parts = [p for p in member.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            if unit == "file":
                if member.endswith("/"):
                    continue
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
                    continue
                else:
                    rel = parts[0]
            units.setdefault(rel, None)
    return list(units.keys())


# --- Hashing & wheel metadata ------------------------------------------------

def sha256_file(path: Path) -> str:
    """Return the lowercase hex sha256 of a file, streamed in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_wheel_metadata(wheel: Path) -> dict:
    """Return {name, version, requires_python, requires} from a wheel METADATA.

    *requires* is a list of (dep_norm, spec, marker). Requirements gated by an
    ``extra`` marker are dropped. Names are normalised.
    """
    with zipfile.ZipFile(wheel) as zf:
        meta_names = [n for n in zf.namelist()
                      if n.endswith(".dist-info/METADATA")]
        if len(meta_names) != 1:
            raise ValueError(
                f"{wheel.name}: expected exactly one dist-info/METADATA, "
                f"found {len(meta_names)}")
        text = zf.read(meta_names[0]).decode("utf-8", "replace")
    msg = Parser().parsestr(text)
    name = (msg.get("Name") or "").strip()
    version = (msg.get("Version") or "").strip()
    requires_python = (msg.get("Requires-Python") or "").strip()
    requires: list[tuple[str, str, str]] = []
    for raw in msg.get_all("Requires-Dist") or []:
        parsed = _parse_requirement(raw)
        if parsed is None:
            continue
        dep, spec, marker = parsed
        if marker and "extra" in marker:
            continue
        requires.append((dep, spec, marker))
    if not name or not version:
        raise ValueError(f"{wheel.name}: METADATA missing Name or Version")
    return {
        "name": normalize(name),
        "version": version,
        "requires_python": requires_python,
        "requires": requires,
    }


def _requires_to_json(requires: list[tuple[str, str, str]]) -> dict:
    """Convert a (dep, spec, marker) list to the manifest ``requires`` object."""
    out: dict = {}
    for dep, spec, marker in requires:
        if marker:
            out[dep] = {"spec": spec, "marker": marker}
        else:
            out[dep] = spec
    return out


def _clause_set(spec: str) -> set[str]:
    """Return the normalised set of clauses in a spec (for comparison)."""
    return {f"{op}{want}" for op, want in parse_spec(spec) if op}


def _join_clauses(specs: list[str]) -> str:
    """Join several specs into one, deduplicating clauses in first-seen order."""
    seen: dict[str, None] = {}
    for spec in specs:
        for op, want in parse_spec(spec):
            if not op:
                continue
            seen.setdefault(f"{op}{want}", None)
    return ",".join(seen.keys())


# --- Generate ----------------------------------------------------------------

# Content archives are named [<component>-]<kind>[-<version>].zip, where <kind>
# is skills, kb or config and "_" may replace "-". Examples:
#   freemaster-skills.zip  freemaster-kb-2.1.0.zip  config.zip  config_2.1.0.zip
CONTENT_NAME_RE = re.compile(
    r"^(?:(?P<comp>[a-z0-9][a-z0-9._-]*?)[-_])?"
    r"(?P<kind>skills|kb|config)"
    r"(?:[-_]v?(?P<ver>\d+(?:\.\d+)*(?:[.-][a-z0-9]+)?))?"
    r"\.zip$",
    re.IGNORECASE,
)


def _classify_archive(filename: str) -> tuple[str, str | None, str | None] | None:
    """Return (kind, component, version) for a content archive name, else None."""
    m = CONTENT_NAME_RE.match(filename)
    if not m:
        return None
    return m.group("kind").lower(), m.group("comp"), m.group("ver")


def _infer_package(component: str | None, package_names: set[str]) -> str | None:
    """Map an archive component (e.g. "freemaster") to a release package name."""
    if not component:
        return None
    norm = normalize(component)
    # Also accept the repo-style component, e.g. "freemaster-mcp".
    base = norm[:-len("-mcp")] if norm.endswith("-mcp") else norm
    for candidate in (norm, "nxp-mcp-" + norm, "nxp-mcp-" + base):
        if candidate in package_names:
            return candidate
    return None



def generate(args) -> int:
    dist = Path(args.dist).resolve()
    if not dist.is_dir():
        print(f"ERROR: --dist directory not found: {dist}", file=sys.stderr)
        return 1

    # A release may be content-only (no wheels, just configs/skills/kb). The
    # "nothing to publish" check happens after content detection, below.
    wheels = sorted(dist.glob("*.whl"))
    for other in list(dist.glob("*.tar.gz")) + list(dist.glob("*.pyz")):
        print(f"note: ignoring {other.name}; the updater does not use it.")


    packages: list[dict] = []
    seen_names: dict[str, str] = {}
    metas: dict[str, dict] = {}
    for wheel in wheels:
        try:
            meta = read_wheel_metadata(wheel)
        except (ValueError, zipfile.BadZipFile) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        name = meta["name"]
        if name in seen_names:
            print(f"ERROR: two wheels for {name} ({seen_names[name]} and "
                  f"{wheel.name}); stale dist/, rebuild with "
                  f"build_modules.py --clean", file=sys.stderr)
            return 1
        seen_names[name] = wheel.name
        metas[name] = meta
        packages.append({
            "name": name,
            "version": meta["version"],
            "asset": wheel.name,
            "sha256": sha256_file(wheel),
            "size": wheel.stat().st_size,
            "requires": _requires_to_json(meta["requires"]),
        })

    package_names = set(seen_names)

    # Content-only release: no wheels, just configs/skills/kb.
    content_only = not packages

    # Version: --version, else the common wheel version. A content-only release
    # has no wheel to take a version from, so --version is required there.
    versions = {meta["version"] for meta in metas.values()}
    if args.version:
        version = args.version
    elif content_only:
        print("ERROR: a content-only release (no wheels) needs --version X.Y.Z",
              file=sys.stderr)
        return 1
    elif len(versions) == 1:
        version = next(iter(versions))
    else:
        print(f"ERROR: wheels disagree on version ({sorted(versions)}); "
              f"pass --version X.Y.Z", file=sys.stderr)
        return 1


    # Python: distinct Requires-Python clauses, comma-joined.
    py_specs = [meta["requires_python"] for meta in metas.values()
                if meta["requires_python"]]
    python = _join_clauses(py_specs) if py_specs else ""

    # dep-repo map: NAME=OWNER/REPO.
    dep_repos: dict[str, str] = {}
    for item in args.dep_repo or []:
        if "=" not in item:
            print(f"ERROR: --dep-repo expects NAME=OWNER/REPO, got {item!r}",
                  file=sys.stderr)
            return 1
        dep_name, _, repo = item.partition("=")
        dep_repos[normalize(dep_name)] = repo.strip()

    # dependencies: requirements not provided by a package of this release.
    errors: list[str] = []
    warnings: list[str] = []
    dep_specs: dict[str, list[str]] = {}
    dep_markers: dict[str, list[str]] = {}
    required_names: set[str] = set()
    for meta in metas.values():
        for dep, spec, marker in meta["requires"]:
            required_names.add(dep)
            if dep in package_names:
                continue
            dep_specs.setdefault(dep, []).append(spec)
            dep_markers.setdefault(dep, []).append(marker)

    dependencies: dict = {}
    for dep in sorted(dep_specs):
        entry: dict = {"spec": _join_clauses(dep_specs[dep])}
        markers = dep_markers[dep]
        if markers and all(m == markers[0] for m in markers) and markers[0]:
            entry["marker"] = markers[0]
        if dep in dep_repos:
            entry["repo"] = dep_repos[dep]
        elif dep.startswith("nxp-mcp"):
            errors.append(
                f"NXP dependency {dep} has no --dep-repo; add "
                f"--dep-repo {dep}=OWNER/REPO")
        dependencies[dep] = entry

    for dep, repo in dep_repos.items():
        if dep not in required_names:
            warnings.append(f"--dep-repo {dep}={repo} but nothing requires {dep}")

    # Content archives.
    content: dict = {}
    config_paths: list[Path] = []
    skill_items: list[tuple[Path, str | None]] = []
    kb_items: list[tuple[Path, str | None]] = []

    if not args.no_auto_content:
        for p in sorted(dist.glob("*.zip")):
            info = _classify_archive(p.name)
            if info is None:
                print(f"note: ignoring {p.name}; expected "
                      f"<component>-skills|kb|config[-X.Y.Z].zip")
                continue
            kind, _component, name_ver = info
            if name_ver and compare_versions(name_ver, version) != 0:
                warnings.append(f"{p.name} is named for version {name_ver} "
                                f"but the release is {version}")
            if kind == "config":
                config_paths.append(p)
            elif kind == "skills":
                skill_items.append((p, None))
            else:
                kb_items.append((p, None))


    for raw in args.configs or []:
        config_paths.append(Path(raw).resolve())
    for raw in args.skills or []:
        path, _, pkg = raw.partition("=")
        skill_items.append((Path(path).resolve(), pkg or None))
    for raw in args.kb or []:
        path, _, pkg = raw.partition("=")
        kb_items.append((Path(path).resolve(), pkg or None))

    # Deduplicate by resolved path (explicit flags override detected ones).
    def _dedup(items):
        seen: dict[str, tuple[Path, str | None]] = {}
        for path, pkg in items:
            key = str(Path(path).resolve())
            prev = seen.get(key)
            # A later explicit package override wins.
            if prev is None or (pkg and not prev[1]):
                seen[key] = (Path(path).resolve(), pkg)
        return list(seen.values())

    config_paths = [p for p, _ in _dedup([(p, None) for p in config_paths])]
    skill_items = _dedup(skill_items)
    kb_items = _dedup(kb_items)

    if len(config_paths) > 1:
        print(f"ERROR: more than one configs archive: "
              f"{[p.name for p in config_paths]}", file=sys.stderr)
        return 1
    if config_paths:
        cfg = config_paths[0]
        if not cfg.is_file():
            print(f"ERROR: configs archive not found: {cfg}", file=sys.stderr)
            return 1
        if cfg.stat().st_size > 2 * GIB:
            warnings.append(f"{cfg.name} is larger than 2 GiB (GitHub limit)")
        content["configs"] = {
            "asset": cfg.name,
            "sha256": sha256_file(cfg),
            "size": cfg.stat().st_size,
        }

    def _content_list(items, kind, unit):
        out = []
        for path, pkg in items:
            if not path.is_file():
                print(f"ERROR: archive not found: {path}", file=sys.stderr)
                return None
            package = pkg
            if content_only:
                # No wheels to match against: the owning package must be given
                # explicitly and is taken as a free (normalised) name.
                if package is None:
                    print(f"ERROR: content-only release: no package to infer "
                          f"for {path.name}; pass "
                          f"--{kind} "
                          f"{path}=<package>", file=sys.stderr)
                    return None
                package = normalize(package)
            else:
                if package is None:
                    info = _classify_archive(path.name)
                    package = _infer_package(info[1] if info else None,
                                             package_names)

                else:
                    package = normalize(package)
                    if package not in package_names:
                        short = "nxp-mcp-" + package
                        package = short if short in package_names else package
                if package is None or package not in package_names:
                    if pkg:
                        # Given explicitly with =PACKAGE but not in this release.
                        print(f"ERROR: {path.name}: package {package} is not "
                              f"a package of this release "
                              f"({', '.join(sorted(package_names))})",
                              file=sys.stderr)
                    else:
                        print(f"ERROR: cannot infer package for {path.name}; "
                              f"pass --{kind} {path}=<package>", file=sys.stderr)
                    return None


            units = _overlay_units(path, unit)
            if not units:
                print(f"ERROR: {path.name} has no replaceable units",
                      file=sys.stderr)
                return None
            if path.stat().st_size > 2 * GIB:
                warnings.append(f"{path.name} is larger than 2 GiB (GitHub limit)")
            out.append({
                "asset": path.name,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "units": units,
                "package": package,
            })
        return out

    if skill_items:
        skills = _content_list(skill_items, "skills", "topdir")
        if skills is None:
            return 1
        content["skills"] = skills
    if kb_items:
        kbs = _content_list(kb_items, "kb", "lance")
        if kbs is None:
            return 1
        content["kb"] = kbs

    if content_only and not content:
        print("ERROR: no wheels and no content archives found; nothing to "
              "publish", file=sys.stderr)
        return 1

    if errors:

        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Build in insertion order so python (when present) sits next to version.
    manifest: dict = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "repo": args.repo,
        "version": version,
    }
    if python:
        manifest["python"] = python
    manifest["packages"] = packages
    manifest["dependencies"] = dependencies
    if content:
        manifest["content"] = content


    out = Path(args.out).resolve() if args.out else (dist / MANIFEST_ASSET_NAME)
    if out.exists() and not args.force:
        print(f"ERROR: {out} already exists; pass --force to overwrite "
              f"(this protects hand edits)", file=sys.stderr)
        return 1
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    # Validate the result with the full --check logic.
    rc = _check_manifest(manifest, dist, args.repo)

    n_github = sum(1 for d in dependencies.values() if d.get("repo"))
    n_index = len(dependencies) - n_github
    print(f"\nManifest written: {out}")
    print(f"  packages: {len(packages)}   dependencies: {len(dependencies)} "
          f"({n_github} from GitHub, {n_index} from index)")
    n_skills = len(content.get("skills", []))
    n_kb = len(content.get("kb", []))
    n_cfg = 1 if content.get("configs") else 0
    print(f"  content:  configs={n_cfg} skills={n_skills} kb={n_kb}")
    tag = f"v{version}"
    assets = " ".join(p["asset"] for p in packages)
    print("Upload (new release):")
    print(f"  gh release create {tag} --repo {args.repo} {assets} "
          f"...other zips... {MANIFEST_ASSET_NAME}")
    print("Upload (existing release):")
    print(f"  gh release upload {tag} --repo {args.repo} --clobber {assets} ...")
    return rc


# --- Shared schema validation (section 3.4) ----------------------------------

def validate_schema(manifest: dict, repo: str | None) -> tuple[list[str], list[str]]:
    """Apply the section 3.4 rules. Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    sv = manifest.get("schema_version")
    if sv != MANIFEST_SCHEMA_VERSION:
        if isinstance(sv, int) and sv > MANIFEST_SCHEMA_VERSION:
            errors.append(f"manifest schema {sv} is newer than this generator "
                          f"supports; update the generator")
        else:
            errors.append(f"schema_version must be {MANIFEST_SCHEMA_VERSION}, "
                          f"got {sv!r}")
        return errors, warnings

    if not isinstance(manifest.get("repo"), str):
        errors.append("repo must be a string")
    elif repo and normalize(manifest["repo"]) != normalize(repo):
        warnings.append(f"manifest repo {manifest['repo']} differs from "
                        f"--repo {repo}")
    if not isinstance(manifest.get("version"), str):
        errors.append("version must be a string")

    packages = manifest.get("packages")
    if not isinstance(packages, list):
        errors.append("packages must be a list")
        return errors, warnings
    # A content-only release carries no wheels (packages == []); that is valid.
    # The "nothing to publish" guard lives in generate(), not here.

    names: set[str] = set()
    assets: set[str] = set()
    pkg_versions: dict[str, str] = {}
    for pkg in packages:
        for field in ("name", "version", "asset", "sha256"):
            if not isinstance(pkg.get(field), str):
                errors.append(f"packages[].{field} must be a string")
        if not isinstance(pkg.get("size"), int):
            errors.append("packages[].size must be an int")
        name = normalize(pkg.get("name", ""))
        if name in names:
            errors.append(f"duplicate package name {name}")
        names.add(name)
        pkg_versions[name] = pkg.get("version", "")
        asset = pkg.get("asset", "")
        if asset in assets:
            errors.append(f"duplicate asset name {asset}")
        assets.add(asset)
        if not isinstance(pkg.get("requires", {}), dict):
            errors.append(f"packages[{name}].requires must be an object")

    dependencies = manifest.get("dependencies", {})
    if not isinstance(dependencies, dict):
        errors.append("dependencies must be an object")
        dependencies = {}

    # Intra-release pins and dependency coverage.
    for pkg in packages:
        name = normalize(pkg.get("name", ""))
        for dep, val in (pkg.get("requires", {}) or {}).items():
            dep_norm = normalize(dep)
            spec = val.get("spec", "") if isinstance(val, dict) else val
            if dep_norm in pkg_versions:
                if not version_satisfies(pkg_versions[dep_norm], spec or ""):
                    errors.append(
                        f"{name} requires {dep_norm}{spec}, but this release "
                        f"ships {dep_norm} {pkg_versions[dep_norm]}")
            elif dep_norm not in {normalize(d) for d in dependencies}:
                errors.append(
                    f"{name} requires {dep_norm} which is neither a package of "
                    f"this release nor listed in dependencies")

    for dep, val in dependencies.items():
        dep_norm = normalize(dep)
        if not isinstance(val, dict) or not isinstance(val.get("spec", ""), str):
            errors.append(f"dependencies[{dep_norm}] must be an object with a "
                          f"string spec")
            continue
        if dep_norm.startswith("nxp-mcp") and not val.get("repo"):
            errors.append(f"dependencies[{dep_norm}] is an NXP package but has "
                          f"no repo")

    content = manifest.get("content", {})
    if content and not isinstance(content, dict):
        errors.append("content must be an object")
        content = {}
    cfg = content.get("configs")
    if cfg is not None and not isinstance(cfg, dict):
        errors.append("content.configs must be an object")
    for kind in ("skills", "kb"):
        items = content.get(kind, [])
        if items and not isinstance(items, list):
            errors.append(f"content.{kind} must be a list")
            continue
        seen_units: set[str] = set()
        for item in items or []:
            units = item.get("units")
            if not isinstance(units, list) or not units:
                errors.append(f"content.{kind} item has empty units")
                units = []
            package = normalize(item.get("package", ""))
            # A content-only release ships no wheels, so there are no package
            # names to match against; keep the (normalised) name as given.
            if names and package not in names:
                short = "nxp-mcp-" + package
                if short in names:
                    item["package"] = short
                else:
                    errors.append(f"content.{kind} item package {package} is "
                                  f"not one of the release packages")
            elif not names:
                item["package"] = package
            for u in units:
                if u in seen_units:
                    errors.append(f"content.{kind} unit {u} appears in two "
                                  f"archives")
                seen_units.add(u)

    return errors, warnings


def _check_manifest(manifest: dict, dist: Path, repo: str | None) -> int:
    """Run the full --check logic. Return 0 on no errors, 1 otherwise."""
    errors, warnings = validate_schema(manifest, repo)

    packages = manifest.get("packages", []) if isinstance(
        manifest.get("packages"), list) else []
    dist_wheels = {p.name for p in dist.glob("*.whl")}
    listed_wheels: set[str] = set()

    # Asset presence, sha256 and size.
    all_items = list(packages)
    content = manifest.get("content", {}) or {}
    if content.get("configs"):
        all_items.append(content["configs"])
    for kind in ("skills", "kb"):
        all_items += content.get(kind, []) or []
    for item in all_items:
        asset = item.get("asset")
        if not asset:
            continue
        if asset.endswith(".whl"):
            listed_wheels.add(asset)
        path = dist / asset
        if not path.is_file():
            errors.append(f"asset {asset} listed in manifest is missing from "
                          f"{dist}")
            continue
        actual_sha = sha256_file(path)
        if item.get("sha256") and actual_sha != item["sha256"]:
            errors.append(f"sha256 mismatch for {asset}: manifest "
                          f"{item['sha256']}, actual {actual_sha}")
        actual_size = path.stat().st_size
        if isinstance(item.get("size"), int) and actual_size != item["size"]:
            errors.append(f"size mismatch for {asset}: manifest {item['size']}, "
                          f"actual {actual_size}")
    for extra in sorted(dist_wheels - listed_wheels):
        warnings.append(f"wheel {extra} in {dist} is not listed in the manifest")

    # requires vs wheel METADATA.
    for pkg in packages:
        asset = pkg.get("asset", "")
        path = dist / asset
        if not path.is_file():
            continue
        try:
            meta = read_wheel_metadata(path)
        except (ValueError, zipfile.BadZipFile) as exc:
            errors.append(str(exc))
            continue
        wheel_req: dict[str, tuple[set[str], str]] = {}
        for dep, spec, marker in meta["requires"]:
            wheel_req[dep] = (_clause_set(spec), marker)
        manifest_req: dict[str, tuple[set[str], str]] = {}
        for dep, val in (pkg.get("requires", {}) or {}).items():
            dep_norm = normalize(dep)
            if isinstance(val, dict):
                manifest_req[dep_norm] = (_clause_set(val.get("spec", "")),
                                          val.get("marker", "") or "")
            else:
                manifest_req[dep_norm] = (_clause_set(val), "")
        for dep in set(wheel_req) | set(manifest_req):
            w = wheel_req.get(dep)
            m = manifest_req.get(dep)
            if w is None:
                errors.append(f"{pkg.get('name')}: manifest requires {dep} "
                              f"which the wheel METADATA does not")
                continue
            if m is None:
                errors.append(f"{pkg.get('name')}: wheel requires {dep} which "
                              f"the manifest does not")
                continue
            w_clauses, w_marker = w
            m_clauses, m_marker = m
            if m_marker != w_marker:
                errors.append(f"{pkg.get('name')}: marker for {dep} differs "
                              f"from the wheel METADATA")
            if m_clauses == w_clauses:
                continue
            if m_clauses.issuperset(w_clauses):
                warnings.append(f"{pkg.get('name')}: manifest spec for {dep} is "
                                f"stricter than the wheel")
            else:
                errors.append(f"{pkg.get('name')}: manifest spec for {dep} "
                              f"differs from the wheel METADATA; pip follows "
                              f"the wheel")

    # dependencies spec == clause union of requiring packages.
    union: dict[str, list[str]] = {}
    pkg_names = {normalize(p.get("name", "")) for p in packages}
    for pkg in packages:
        for dep, val in (pkg.get("requires", {}) or {}).items():
            dep_norm = normalize(dep)
            if dep_norm in pkg_names:
                continue
            spec = val.get("spec", "") if isinstance(val, dict) else val
            union.setdefault(dep_norm, []).append(spec)
    for dep, val in (manifest.get("dependencies", {}) or {}).items():
        dep_norm = normalize(dep)
        spec = val.get("spec", "") if isinstance(val, dict) else ""
        expected = _clause_set(_join_clauses(union.get(dep_norm, [])))
        if _clause_set(spec) != expected:
            errors.append(f"dependencies[{dep_norm}] spec {spec} does not equal "
                          f"the clause union of its requiring packages")

    # Intra-release third-party conflicts.
    for dep, specs in union.items():
        exacts = [spec_pins_exact(s) for s in specs]
        exacts = [e for e in exacts if e]
        if len({e for e in exacts}) > 1:
            errors.append(f"intra-release conflict on {dep}: different == pins "
                          f"{sorted(set(exacts))}")
            continue
        if exacts:
            pin = exacts[0]
            for s in specs:
                if not version_satisfies(pin, s):
                    errors.append(f"intra-release conflict on {dep}: {pin} does "
                                  f"not satisfy {s}")
                    break

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return 1 if errors else 0


def check(args) -> int:
    dist = Path(args.dist).resolve()
    manifest_path = (Path(args.manifest).resolve() if args.manifest
                     else dist / MANIFEST_ASSET_NAME)
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: manifest is not valid JSON: {exc}", file=sys.stderr)
        return 1
    return _check_manifest(manifest, dist, getattr(args, "repo", None))


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_release_manifest.py",
        description="Generate or validate release-manifest.json for a release.",

        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--repo", metavar="OWNER/NAME",
                        help="GitHub repo as OWNER/NAME (required to generate).")
    parser.add_argument("--dist", metavar="DIR", default="dist",
                        help="directory holding the wheels / zips (default: ./dist).")
    parser.add_argument("--out", metavar="FILE",
                        help="output manifest (default: <dist>/release-manifest.json).")

    parser.add_argument("--version", metavar="X.Y.Z",
                        help="release version (default: common wheel version).")
    parser.add_argument("--dep-repo", metavar="NAME=OWNER/REPO", action="append",
                        default=[],
                        help="GitHub repo of a cross-repo NXP dependency. Repeatable.")
    parser.add_argument("--configs", metavar="PATH", action="append", default=[],
                        help="explicit configs archive (max 1).")
    parser.add_argument("--skills", metavar="PATH[=PACKAGE]", action="append",
                        default=[], help="explicit skills archive(s). Repeatable.")
    parser.add_argument("--kb", metavar="PATH[=PACKAGE]", action="append",
                        default=[], help="explicit kb archive(s). Repeatable.")
    parser.add_argument("--no-auto-content", action="store_true",
                        help="do not auto-detect <comp>-skills/-kb/-config zips.")                        
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing --out file.")
    parser.add_argument("--check", action="store_true",
                        help="validate an existing manifest against --dist.")
    parser.add_argument("--manifest", metavar="FILE",
                        help="manifest to validate with --check.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check:
        return check(args)
    if not args.repo:
        parser.error("--repo is required to generate a manifest")
    return generate(args)


if __name__ == "__main__":
    sys.exit(main())
