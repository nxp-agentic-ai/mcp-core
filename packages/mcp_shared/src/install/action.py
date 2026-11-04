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

"""MCP action adapters for the shared install layer.

A server exposes provisioning by merging the actions built here into its
catalog:

* ``tool.install_offers`` - read-only; how could this be installed, and at what
  cost.
* ``tool.install`` - install a named component.
* ``tool.install_from_artifact`` - install whatever a given file or folder is.
  This is the one that matches how users actually ask ("install DS from
  <this exe>"), since the artifact decides both the method and the product.

All are served locally by the MCP server: they must work precisely when the
product is missing, so they never require the tool to be running.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..action.models import (
    ActionContract,
    ActionExecutable,
    ActionParameter,
)
from ..detect import DETECTORS, S32DS_COMPONENT
from .guide import with_guidance
from .models import (
    InstallMode,
    InstallRequest,
    InstallStatus,
)
from .registry import InstallProviderRegistry
from .sources_catalog import P2_REQUIRES_S32DS_NOTE, candidates_for

_OFFERS_ACTION_NAME = "tool.install_offers"
_INSTALL_ACTION_NAME = "tool.install"
_ARTIFACT_ACTION_NAME = "tool.install_from_artifact"


def _s32ds_candidates() -> tuple[str, ...]:
    """Existing S32 Design Studio installations, newest first.

    Offered as the choices for the "which S32DS?" question. Detection cannot
    fail the install: with nothing found the question becomes a free-form path
    that the user answers.
    """

    return tuple(tool.root for tool in DETECTORS.find_all(S32DS_COMPONENT))


def _snapshot(params: dict[str, Any]) -> dict[str, str | None]:
    """The installed-components snapshot to reason about.

    A caller-supplied snapshot wins, because it may describe components this
    server cannot detect. Otherwise the machine is scanned, so a workflow that
    simply hit a missing tool does not have to establish what is installed
    before it can ask how to install the rest.
    """

    supplied = params.get("installed")
    if supplied:
        return dict(supplied)
    return DETECTORS.snapshot()


def build_install_from_artifact_action(
    registry: InstallProviderRegistry,
    *,
    name: str = _ARTIFACT_ACTION_NAME,
) -> ActionExecutable:
    """Install whatever a downloaded artifact happens to be.

    This is the action to use when the user names a file rather than a product:
    the artifact's shape selects the install method and its filename selects the
    provider, so neither the agent nor the user has to state either.
    """

    def _execute(params: dict[str, Any]) -> dict[str, Any]:
        location = params["path"]
        installed = _snapshot(params)

        match = registry.match_artifact(location, installed=installed)
        if not match.ok or match.offer is None or match.provider is None:
            return {
                "status": str(InstallStatus.UNSUPPORTED),
                "component": "",
                "provider": "",
                "error": match.error,
                "artifactKind": str(match.source.kind),
            }

        offer = match.offer
        source = match.source

        request = InstallRequest(
            component=offer.component,
            provider=offer.provider,
            # Exactly one of these is meaningful, decided by the artifact shape.
            installer_path=str(source.path) if source.repository == "" else None,
            repository=source.repository or None,
            install_dir=params.get("installDir"),
            mode=InstallMode(params.get("mode") or InstallMode.TYPICAL),
            components=params.get("componentSelections") or {},
            extra_properties=params.get("extraProperties") or {},
            s32ds_path=params.get("s32dsPath") or installed.get(S32DS_COMPONENT),
            iu_ids=tuple(params.get("iuIds") or ()),
            select_ius=bool(params.get("selectIus", False)),
            wait=bool(params.get("wait", True)),
            timeout_ms=int(params.get("timeoutMs", 30 * 60 * 1000)),
            confirm=bool(params.get("confirm", True)),
            dry_run=bool(params.get("dryRun", False)),
        )
        result = with_guidance(
            match.provider.install(request),
            action_name=name,
            installer_param="path",
            s32ds_candidates=_s32ds_candidates(),
        )
        payload = result.to_dict()
        payload["artifactKind"] = str(source.kind)
        if source.repository:
            payload["repository"] = source.repository
        return payload

    async def handler(params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(_execute, params or {})

    return ActionExecutable(
        contract=ActionContract(
            name=name,
            description=(
                "MANDATORY PREREQUISITE: read the 's32ds-install-tools' skill in full "
                "before invoking this action. It is published by this same MCP server "
                "as a skill resource - read "
                "skill://nxp_s32ds/s32ds-install-tools/SKILL.md via access_mcp_resource. "
                "Do not look for it on the local filesystem. "
                "Install, upgrade or provision an NXP tool from a downloaded artifact - "
                "a product installer or setup exe, a folder containing one or several, "
                "or an Eclipse p2 update-site zip or folder. This is the action for "
                "'install DS from here <path>', 'install S32DS from my Downloads', "
                "'run this setup silently', 'unattended install', 'install this update "
                "site zip', 'upgrade S32DS'. The artifact decides the install method and "
                "the product, so the component does not have to be named; the installer "
                "flags, silent/unattended mode, install prefix and installer.properties "
                "are all rendered here, so never hand-write an installer command line in "
                "a shell instead. Update-site artifacts are installed into an existing "
                "S32 Design Studio, so s32dsPath is required for those. Runs the "
                "install directly; pass dryRun=true only to preview the command."
            ),
            params=(
                ActionParameter(
                    name="path",
                    required=True,
                    schema={
                        "type": "string",
                        "description": (
                            "Path to the artifact: an installer executable, a folder "
                            "containing one, an update-site .zip, or an unpacked "
                            "update-site folder."
                        ),
                    },
                ),
                ActionParameter(
                    name="s32dsPath",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Existing S32 Design Studio installation to install into, "
                            "e.g. 'C:/NXP/S32DS.3.6.4'. Required for update-site "
                            "artifacts."
                        ),
                    },
                ),
                ActionParameter(
                    name="installDir",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Target directory for an installer artifact. Omit to use the "
                            "installer default."
                        ),
                    },
                ),
                ActionParameter(
                    name="mode",
                    required=False,
                    schema={
                        "type": "string",
                        "enum": [str(mode) for mode in InstallMode],
                        "description": (
                            "Installer preset: 'typical' (default), 'custom' (honors "
                            "componentSelections), or 'upgrade' (updates the install "
                            "named by installDir)."
                        ),
                    },
                ),
                ActionParameter(
                    name="componentSelections",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": "boolean"},
                        "description": "Per-component opt in/out for custom or upgrade mode.",
                    },
                ),
                ActionParameter(
                    name="extraProperties",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Additional raw installer.properties entries.",
                    },
                ),
                ActionParameter(
                    name="iuIds",
                    required=False,
                    schema={
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Subset of installable units to take from an update-site "
                            "artifact. Omit to discover and install every root feature "
                            "the site publishes."
                        ),
                    },
                ),
                ActionParameter(
                    name="selectIus",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": (
                            "Return the update site's installable units for the user to "
                            "choose from instead of installing all of them. Nothing is "
                            "installed; re-call with the chosen ids in iuIds."
                        ),
                    },
                ),
                ActionParameter(
                    name="wait",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": "Block until the installer finishes (default true).",
                    },
                ),
                ActionParameter(
                    name="timeoutMs",
                    required=False,
                    schema={
                        "type": "integer",
                        "minimum": 0,
                        "description": "Maximum install time. Default 1800000 ms.",
                    },
                ),
                ActionParameter(
                    name="confirm",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": (
                            "Default true: run the install. False returns the planned "
                            "command instead."
                        ),
                    },
                ),
                ActionParameter(
                    name="dryRun",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": "Return the command, run nothing.",
                    },
                ),
                ActionParameter(
                    name="installed",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": ["string", "null"]},
                        "description": (
                            "Detection snapshot: component -> install path. An 's32ds' "
                            "entry is used as the default target for update sites."
                        ),
                    },
                ),
            ),
            category="lifecycle",
            example={
                "params": {
                    "path": "D:/downloads/SW32_S32DS_3.6.10_RFP_D2607_win32.x86_64.exe",
                }
            },
            preconditions=(
                "The artifact is already downloaded; this action does not fetch it.",
                "On Windows the MCP server process must run elevated.",
            ),
            workflow_hints=(
                "Use this when the user names a file or folder to install from.",
                "Call once; the install runs when the inputs are valid. Use dryRun=true "
                "only if the user asks to preview the command.",
                "status='requires_existing_s32ds' means an update site was given with no "
                "S32 Design Studio to install it into; ask for s32dsPath.",
            ),
            related_actions=(_OFFERS_ACTION_NAME, _INSTALL_ACTION_NAME),
        ),
        handler=handler,
    )


def build_install_offers_action(
    registry: InstallProviderRegistry,
    *,
    name: str = _OFFERS_ACTION_NAME,
) -> ActionExecutable:
    """Read-only action listing ranked install offers and the resulting plan."""

    def _execute(params: dict[str, Any]) -> dict[str, Any]:
        components = params.get("components") or (
            [params["component"]] if params.get("component") else []
        )
        installed = _snapshot(params)

        if not components:
            return {
                "components": list(registry.components()),
                "providers": list(registry.list_providers()),
                "offers": [offer.to_dict() for offer in registry.list_offers()],
                "installed": installed,
            }

        plan = registry.plan(components, installed=installed)
        return {
            "plan": plan.to_dict(),
            # What the ranking was based on, so the user can see whether a tool
            # was treated as already present.
            "installed": installed,
            "missing": sorted(
                component for component in components if not installed.get(component)
            ),
            "offersByComponent": {
                component: [
                    offer.to_dict()
                    for offer in registry.offers_of(component, installed=installed)
                ]
                for component in components
            },
            # Where the user can download each artifact from. Listed up front so
            # the download can start while the options are still being discussed.
            "sourcesByComponent": {
                component: [
                    candidate.to_dict() for candidate in candidates_for(component)
                ]
                for component in components
            },
            "sourceGuidance": P2_REQUIRES_S32DS_NOTE,
        }

    async def handler(params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(_execute, params or {})

    return ActionExecutable(
        contract=ActionContract(
            name=name,
            description=(
                "MANDATORY PREREQUISITE: read the 's32ds-install-tools' skill in "
                "full before invoking this action. It is published by this same MCP "
                "server as a skill resource - read "
                "skill://nxp_s32ds/s32ds-install-tools/SKILL.md via access_mcp_resource. "
                "List how the requested NXP tool components (S32 Design Studio / 'DS', "
                "S32 Debugger, S32CT, S32 Trace, GCC, RTD and other update-site "
                "components) could be installed, provisioned or upgraded on this machine, "
                "ranked best-first, together with an ordered install plan and the "
                "locations to download each installer from. Read-only: nothing is "
                "installed. Call this when a workflow cannot proceed because a tool is "
                "missing, or when the user asks which installer to run, and present the "
                "options before calling tool.install."
            ),
            params=(
                ActionParameter(
                    name="component",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Single component to look up, e.g. 's32ds', 's32debugger'. "
                            "Omit both this and 'components' to list the whole catalog."
                        ),
                    },
                ),
                ActionParameter(
                    name="components",
                    required=False,
                    schema={
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Several components to satisfy together. Components delivered "
                            "by one installer are collapsed into a single step."
                        ),
                    },
                ),
                ActionParameter(
                    name="installed",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": ["string", "null"]},
                        "description": (
                            "Detection snapshot: component name -> existing install path, "
                            "or null when absent. Omit it to scan this machine. Supply it "
                            "to override what was detected, or to describe components "
                            "this server cannot detect."
                        ),
                    },
                ),
            ),
            category="inspect",
            workflow_hints=(
                "Call this before tool.install so the user can choose between a "
                "standalone install and adding the component to S32 Design Studio.",
                "A plan step with requiresInstallerFile=true needs an installer path "
                "from the user; ask for it rather than guessing one.",
            ),
            related_actions=(_INSTALL_ACTION_NAME,),
        ),
        handler=handler,
    )


def build_install_action(
    registry: InstallProviderRegistry,
    *,
    name: str = _INSTALL_ACTION_NAME,
) -> ActionExecutable:
    """Action performing one install step."""

    def _execute(params: dict[str, Any]) -> dict[str, Any]:
        component = params["component"]
        provider_name = params.get("provider")

        if provider_name:
            provider = registry.get(provider_name)
            if provider is None:
                return _error(
                    component,
                    provider_name,
                    f"Unknown install provider '{provider_name}'. "
                    f"Known: {', '.join(registry.list_providers()) or 'none'}.",
                )
        else:
            offer = registry.best_offer(component, installed=_snapshot(params))
            if offer is None:
                return _error(
                    component,
                    "",
                    f"No install provider is registered for component '{component}'. "
                    f"Known components: {', '.join(registry.components()) or 'none'}.",
                )
            provider = registry.get(offer.provider)
            if provider is None:
                return _error(component, offer.provider, "Offer references an unknown provider.")

        request = InstallRequest(
            component=component,
            provider=provider.PROVIDER,
            installer_path=params.get("installerPath"),
            install_dir=params.get("installDir"),
            mode=InstallMode(params.get("mode") or InstallMode.TYPICAL),
            components=params.get("componentSelections") or {},
            extra_properties=params.get("extraProperties") or {},
            properties_path=params.get("propertiesPath"),
            s32ds_path=params.get("s32dsPath"),
            repository=params.get("repository"),
            iu_ids=tuple(params.get("iuIds") or ()),
            select_ius=bool(params.get("selectIus", False)),
            wait=bool(params.get("wait", True)),
            timeout_ms=int(params.get("timeoutMs", 30 * 60 * 1000)),
            confirm=bool(params.get("confirm", True)),
            dry_run=bool(params.get("dryRun", False)),
        )
        return with_guidance(
            provider.install(request),
            action_name=name,
            installer_param="installerPath",
            s32ds_candidates=_s32ds_candidates(),
        ).to_dict()

    async def handler(params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(_execute, params or {})

    return ActionExecutable(
        contract=ActionContract(
            name=name,
            description=(
                "MANDATORY PREREQUISITE: read the 's32ds-install-tools' skill in "
                "full before invoking this action. It is published by this same MCP "
                "server as a skill resource - read "
                "skill://nxp_s32ds/s32ds-install-tools/SKILL.md via access_mcp_resource. "
                "Install, upgrade or provision one named NXP tool component "
                "non-interactively - silent/unattended, with the installer flags, "
                "install prefix and installer.properties rendered here, so never "
                "hand-write an installer command line in a shell instead. Prefer "
                "tool.install_from_artifact when the user simply points at a file or "
                "folder. Runs the install directly; pass dryRun=true only to preview "
                "the command and installer properties. Use tool.install_offers to "
                "choose the provider before calling this."
            ),
            params=(
                ActionParameter(
                    name="component",
                    required=True,
                    schema={
                        "type": "string",
                        "description": "Component to install, e.g. 's32ds', 's32debugger'.",
                    },
                ),
                ActionParameter(
                    name="provider",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Provider to use, from tool.install_offers. Omit to take the "
                            "best-ranked offer."
                        ),
                    },
                ),
                ActionParameter(
                    name="installerPath",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Path to the product installer, required for exe_installer "
                            "offers. The filename must match the provider's expected "
                            "pattern; arbitrary executables are refused."
                        ),
                    },
                ),
                ActionParameter(
                    name="installDir",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Target directory (USER_INSTALL_DIR). Omit to use the "
                            "installer default. Required for mode='upgrade', where it "
                            "must name the existing installation."
                        ),
                    },
                ),
                ActionParameter(
                    name="mode",
                    required=False,
                    schema={
                        "type": "string",
                        "enum": [str(mode) for mode in InstallMode],
                        "description": (
                            "'typical' (default) installs the predefined component set; "
                            "'custom' honors componentSelections; 'upgrade' updates an "
                            "existing installation in place."
                        ),
                    },
                ),
                ActionParameter(
                    name="componentSelections",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": "boolean"},
                        "description": (
                            "Per-component opt in/out for mode='custom' or 'upgrade', "
                            "e.g. {'gcc': true, 'segger_drivers': false}."
                        ),
                    },
                ),
                ActionParameter(
                    name="extraProperties",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Additional raw installer.properties entries.",
                    },
                ),
                ActionParameter(
                    name="propertiesPath",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Where to write the generated properties file. Defaults to a "
                            "temporary file."
                        ),
                    },
                ),
                ActionParameter(
                    name="s32dsPath",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Existing S32 Design Studio installation to install into. "
                            "Required for p2_update_site offers."
                        ),
                    },
                ),
                ActionParameter(
                    name="repository",
                    required=False,
                    schema={
                        "type": "string",
                        "description": "Override the provider's p2 update-site URL.",
                    },
                ),
                ActionParameter(
                    name="selectIus",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": (
                            "Return the update site's installable units for the user to "
                            "choose from instead of installing all of them. Nothing is "
                            "installed; re-call with the chosen ids in iuIds."
                        ),
                    },
                ),
                ActionParameter(
                    name="iuIds",
                    required=False,
                    schema={
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Subset of p2 installable units to install. Omit to install "
                            "every root feature the update site publishes."
                        ),
                    },
                ),
                ActionParameter(
                    name="wait",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": (
                            "Block until the installer finishes (default true). With "
                            "false the installer is detached and the outcome is unknown."
                        ),
                    },
                ),
                ActionParameter(
                    name="timeoutMs",
                    required=False,
                    schema={
                        "type": "integer",
                        "minimum": 0,
                        "description": "Maximum install time. Default 1800000 ms.",
                    },
                ),
                ActionParameter(
                    name="confirm",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": (
                            "Default true: run the installer. False returns the planned "
                            "command and properties instead."
                        ),
                    },
                ),
                ActionParameter(
                    name="dryRun",
                    required=False,
                    schema={
                        "type": "boolean",
                        "description": "Return the command and properties, run nothing.",
                    },
                ),
                ActionParameter(
                    name="installed",
                    required=False,
                    schema={
                        "type": "object",
                        "additionalProperties": {"type": ["string", "null"]},
                        "description": (
                            "Detection snapshot used to pick the offer when 'provider' "
                            "is omitted."
                        ),
                    },
                ),
            ),
            category="lifecycle",
            example={
                "params": {
                    "component": "s32ds",
                    "installerPath": "D:/downloads/S32DS.3.6.4_win32.x86_64.exe",
                    "installDir": "C:/NXP/S32DS.3.6.4",
                    "confirm": True,
                }
            },
            preconditions=(
                "For exe_installer offers the installer file is already on disk; this "
                "action does not download it.",
                "On Windows the MCP server process must run elevated.",
            ),
            workflow_hints=(
                "Call tool.install_offers first and show the user the ranked options.",
                "Call once; the install runs when the inputs are valid. Use dryRun=true "
                "only if the user asks to preview the command.",
                "status='requires_installer_file' means the user must supply or download "
                "the installer; status='requires_existing_s32ds' means S32 Design Studio "
                "has to be installed first.",
            ),
            related_actions=(_OFFERS_ACTION_NAME,),
        ),
        handler=handler,
    )


def build_install_actions(
    registry: InstallProviderRegistry,
) -> tuple[ActionExecutable, ...]:
    """Build every provisioning action for one registry."""

    return (
        build_install_offers_action(registry),
        build_install_action(registry),
        build_install_from_artifact_action(registry),
    )


def _error(component: str, provider: str, message: str) -> dict[str, Any]:
    return {
        "status": str(InstallStatus.UNSUPPORTED),
        "component": component,
        "provider": provider,
        "error": message,
    }
