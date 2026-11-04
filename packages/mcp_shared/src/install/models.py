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

"""Data models for the shared tool-provisioning (install) layer.

Every provider returns the same :class:`InstallResult` envelope regardless of
the install method, so an agent can reason about "was the tool installed?"
without knowing which product it asked for.

Installation is always non-interactive. There is no GUI or console install
mode: ``INSTALLER_UI`` is pinned to ``silent`` and p2 invocations always pass
``-nosplash``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class InstallMethod(StrEnum):
    """How a provider delivers a component."""

    # Product ships its own installer executable (InstallAnywhere, silent mode).
    # Covers both fresh installs and upgrades of that product.
    EXE_INSTALLER = "exe_installer"

    # Component is delivered into an already-installed S32 Design Studio via an
    # Eclipse p2 update site (Extensions and Updates, SDKs, RTD, add-ons).
    P2_UPDATE_SITE = "p2_update_site"

    # Cannot be automated; the caller is pointed at a download location.
    GUIDANCE_ONLY = "guidance_only"


class InstallDelivery(StrEnum):
    """Where the component ends up."""

    STANDALONE = "standalone"
    INTO_S32DS = "into_s32ds"


class InstallStatus(StrEnum):
    """Terminal state of an install attempt."""

    INSTALLED = "installed"
    STARTED = "started"                              # wait=False, outcome unknown
    REQUIRES_CONFIRMATION = "requires_confirmation"  # confirm=False: plan only
    REQUIRES_INSTALLER_FILE = "requires_installer_file"
    REQUIRES_EXISTING_S32DS = "requires_existing_s32ds"
    # No location known for the artifact; ask the user or offer suggestions.
    REQUIRES_SOURCE = "requires_source"
    # The source offers several installable products; the user must choose.
    REQUIRES_SELECTION = "requires_selection"
    GUIDANCE_ONLY = "guidance_only"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class InstallMode(StrEnum):
    """Installer property preset for :data:`InstallMethod.EXE_INSTALLER`."""

    TYPICAL = "typical"
    CUSTOM = "custom"
    UPGRADE = "upgrade"


class QuestionKind(StrEnum):
    """What an answer to an :class:`InstallQuestion` has to look like."""

    PATH = "path"                  # a local file or folder
    CHOICE = "choice"              # exactly one of ``choices``
    MULTI_CHOICE = "multi_choice"  # zero or more of ``choices``
    CONFIRM = "confirm"            # boolean


@dataclass(frozen=True, slots=True)
class InstallQuestion:
    """One thing the user must answer before an install can continue.

    Questions are derived from the status of a completed call, so guidance is
    stateless: the agent re-invokes the same action with the answer filled in,
    and the server keeps no session.

    ``param`` names the action parameter the answer belongs in, which is what
    lets an agent apply an answer without a lookup table.
    """

    id: str
    prompt: str
    kind: QuestionKind
    param: str = ""
    choices: tuple[str, ...] = ()
    default: str = ""
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "prompt": self.prompt,
            "kind": str(self.kind),
            "param": self.param or self.id,
        }
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.default:
            payload["default"] = self.default
        if self.why:
            payload["why"] = self.why
        return payload


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """A place the user can obtain an install artifact from.

    Candidates are presented, never fetched: the user downloads the installer or
    update-site zip and passes back a local path. ``manual_download`` is true for
    everything coming out of the catalog, because a product download page is a
    page, not an artifact.
    """

    url: str
    label: str = ""
    # Reuses the trust ordering of :class:`InstallSource`, as a plain string so
    # this model stays independent of the source-resolution module.
    origin: str = "catalog"
    # Free-form: "installer", "update_site", "page".
    kind: str = "page"
    manual_download: bool = True
    requires_auth: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "label": self.label,
            "origin": self.origin,
            "kind": self.kind,
            "manualDownload": self.manual_download,
        }
        if self.requires_auth:
            payload["requiresAuth"] = True
        if self.notes:
            payload["notes"] = self.notes
        return payload


# Component name -> install root of an existing install, or None when absent.
# Produced by the detection layer; kept as a plain mapping so the install layer
# does not depend on it.
InstalledSnapshot = Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class InstallOffer:
    """One way to obtain one component.

    A component may have several offers - e.g. a debugger available both as a
    standalone product and bundled in the S32 Design Studio installer. The
    registry ranks them; it never silently picks one.
    """

    component: str
    provider: str
    method: InstallMethod
    delivery: InstallDelivery
    display_name: str = ""
    # Everything a single run of this offer installs. Lets the registry collapse
    # several requested components into one installer invocation.
    supplies: frozenset[str] = field(default_factory=frozenset)
    requires_installer_file: bool = False
    requires_existing_s32ds: bool = False
    requires_elevation: bool = False
    download_url: str = ""
    # Every place this offer's artifact can be obtained from, best first. All of
    # them are manual downloads: nothing here is fetched automatically.
    sources: tuple[SourceCandidate, ...] = ()
    installer_glob: tuple[str, ...] = ()
    notes: str = ""
    # Lower sorts first.
    rank: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "provider": self.provider,
            "displayName": self.display_name,
            "method": str(self.method),
            "delivery": str(self.delivery),
            "supplies": sorted(self.supplies),
            "requiresInstallerFile": self.requires_installer_file,
            "requiresExistingS32ds": self.requires_existing_s32ds,
            "requiresElevation": self.requires_elevation,
            "downloadUrl": self.download_url,
            "installerGlob": list(self.installer_glob),
            "notes": self.notes,
            "rank": self.rank,
        }


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Everything a provider needs to perform one install.

    The install runs as soon as the inputs are valid. ``dry_run`` (or an
    explicit ``confirm=False``) returns :data:`InstallStatus.REQUIRES_CONFIRMATION`
    with the exact command and rendered properties instead of running it.
    """

    component: str
    provider: str | None = None

    # EXE_INSTALLER inputs
    installer_path: str | None = None
    install_dir: str | None = None
    mode: InstallMode = InstallMode.TYPICAL
    components: Mapping[str, bool] = field(default_factory=dict)
    extra_properties: Mapping[str, str] = field(default_factory=dict)
    properties_path: str | None = None

    # P2_UPDATE_SITE inputs
    s32ds_path: str | None = None
    repository: str | None = None
    # Subset to install. Empty means every root feature the site publishes.
    iu_ids: tuple[str, ...] = ()
    # Return the site's installable units for review instead of installing them.
    select_ius: bool = False

    wait: bool = True
    timeout_ms: int = 30 * 60 * 1000
    confirm: bool = True
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Ordered set of offers that together satisfy the requested components."""

    requested: tuple[str, ...]
    steps: tuple[InstallOffer, ...]
    rationale: str = ""
    # Reasons the plan cannot be executed as-is (e.g. no installer file given).
    blocking: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return bool(self.steps) and not self.blocking and not self.unresolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested),
            "steps": [step.to_dict() for step in self.steps],
            "rationale": self.rationale,
            "blocking": list(self.blocking),
            "unresolved": list(self.unresolved),
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class SkippedUnit:
    """A site feature left out of a retry because S32DS already has it."""

    id: str
    site_version: str = ""
    installed_version: str = ""
    # "installed_roots" (p2 profile) or "features_folder" (eclipse/features).
    found_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "siteVersion": self.site_version,
            "installedVersion": self.installed_version,
            "foundBy": self.found_by,
        }


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Uniform outcome envelope shared by every provider and method."""

    status: InstallStatus
    component: str
    provider: str
    method: InstallMethod
    delivery: InstallDelivery | None = None
    command: tuple[str, ...] = ()
    properties_path: str | None = None
    properties_text: str | None = None
    return_code: int | None = None
    elapsed_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    installed_path: str | None = None
    guidance: str = ""
    error: str = ""
    # Populated with REQUIRES_SELECTION: the installable units the source
    # publishes, when more than one product is available.
    candidates: tuple[str, ...] = ()
    # Populated with REQUIRES_SOURCE: locations the user could supply. These are
    # suggestions only and are never acted on without the user choosing one.
    suggested_sources: tuple[str, ...] = ()
    # Labelled download locations for the same situation, carrying where each
    # came from and whether it needs authentication. The user downloads from one
    # of these and passes the local path back.
    sources: tuple[SourceCandidate, ...] = ()
    # What the user has to answer for this call to be retried successfully.
    questions: tuple[InstallQuestion, ...] = ()
    # One sentence telling the agent what to do with the answers.
    next_step: str = ""
    # p2 only: what the final -installIU carried, and what a conflict retry left out.
    installed_ius: tuple[str, ...] = ()
    skipped_ius: tuple[SkippedUnit, ...] = ()
    retried_missing_only: bool = False
    first_attempt_error: str = ""

    @property
    def success(self) -> bool:
        return self.status is InstallStatus.INSTALLED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": str(self.status),
            "component": self.component,
            "provider": self.provider,
            "method": str(self.method),
            "elapsedMs": self.elapsed_ms,
        }
        if self.delivery is not None:
            payload["delivery"] = str(self.delivery)
        if self.command:
            payload["command"] = list(self.command)
        if self.properties_path:
            payload["propertiesPath"] = self.properties_path
        if self.properties_text is not None:
            payload["propertiesText"] = self.properties_text
        if self.return_code is not None:
            payload["returnCode"] = self.return_code
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.installed_path:
            payload["installedPath"] = self.installed_path
        if self.guidance:
            payload["guidance"] = self.guidance
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        if self.suggested_sources:
            payload["suggestedSources"] = list(self.suggested_sources)
        if self.sources:
            payload["sources"] = [candidate.to_dict() for candidate in self.sources]
        if self.questions:
            payload["questions"] = [question.to_dict() for question in self.questions]
        if self.next_step:
            payload["nextStep"] = self.next_step
        if self.installed_ius:
            payload["installedIus"] = list(self.installed_ius)
        if self.skipped_ius:
            payload["skippedIus"] = [unit.to_dict() for unit in self.skipped_ius]
        if self.retried_missing_only:
            payload["retriedMissingOnly"] = True
        if self.first_attempt_error:
            payload["firstAttemptError"] = self.first_attempt_error
        if self.error:
            payload["error"] = self.error
        return payload


