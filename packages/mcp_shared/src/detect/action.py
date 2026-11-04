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

"""MCP action adapter for tool detection."""

from __future__ import annotations

import asyncio
from typing import Any

from ..action.models import ActionContract, ActionExecutable, ActionParameter
from .engine import DETECTORS, DetectorRegistry

DETECT_ACTION_NAME = "tool.detect"


def build_detect_action(
    registry: DetectorRegistry | None = None,
    *,
    name: str = DETECT_ACTION_NAME,
) -> ActionExecutable:
    """Read-only action reporting which NXP tools are present on this machine."""

    detectors = registry or DETECTORS

    def _execute(params: dict[str, Any]) -> dict[str, Any]:
        components = params.get("components") or (
            [params["component"]] if params.get("component") else None
        )
        detail = detectors.report(components)
        snapshot = detectors.snapshot(components)
        return {
            "detected": detail,
            # Ready to pass straight into tool.install_offers / tool.install.
            "installed": snapshot,
            "missing": sorted(key for key, value in snapshot.items() if not value),
            "knownComponents": list(detectors.components()),
        }

    async def handler(params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(_execute, params or {})

    return ActionExecutable(
        contract=ActionContract(
            name=name,
            description=(
                "Report which NXP tools are installed on this machine and where, by "
                "scanning the standard installation roots for the product directories "
                "and executables. Read-only. Call this when a workflow needs a tool and "
                "it is not known whether it is present, or when the user asks what is "
                "installed, which S32 Design Studio / 'DS' versions exist, or where a "
                "tool lives. Pass the returned 'installed' map into tool.install_offers "
                "to get install options for what is missing. To actually install, "
                "upgrade or provision a tool, use tool.install_from_artifact or "
                "tool.install and read the 's32ds-install-tools' skill first."
            ),
            params=(
                ActionParameter(
                    name="component",
                    required=False,
                    schema={
                        "type": "string",
                        "description": (
                            "Single component to look for, e.g. 's32ds'. Omit both this "
                            "and 'components' to detect everything known."
                        ),
                    },
                ),
                ActionParameter(
                    name="components",
                    required=False,
                    schema={
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Several components to look for in one call.",
                    },
                ),
            ),
            category="inspect",
            example={"params": {"component": "s32ds"}},
            workflow_hints=(
                "A component reported with known=false has no detector registered; "
                "that means 'cannot tell', not 'not installed'.",
                "Several installations of one component are returned newest first; do "
                "not pick one for the user when a workflow needs a specific version.",
            ),
            related_actions=("tool.install_offers", "tool.install"),
        ),
        handler=handler,
    )
