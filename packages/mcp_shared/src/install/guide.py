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

"""Turns a blocked install result into the questions that would unblock it.

Every non-terminal :class:`InstallStatus` already says *what* is missing. This
module says *what to ask the user*, and which action parameter the answer goes
into, so an agent can continue without a hardcoded mapping per status.

Guidance is stateless by design. Nothing is stored between calls: the agent
collects the answers, puts them in the same action's parameters, and calls
again. There is no session to expire, resume, or clean up, and a half-answered
flow cannot drift out of sync with the real state of the machine.

Installs run as soon as their inputs are complete, so questions only cover
missing inputs. A dry-run preview is not a question: it is what the caller
asked for.
"""

from __future__ import annotations

import dataclasses

from .models import (
    InstallQuestion,
    InstallResult,
    InstallStatus,
    QuestionKind,
    SourceCandidate,
)
from .sources_catalog import candidates_for

# Statuses that mean "the install can proceed once the user supplies something".
_ACTIONABLE = frozenset(
    {
        InstallStatus.REQUIRES_SOURCE,
        InstallStatus.REQUIRES_INSTALLER_FILE,
        InstallStatus.REQUIRES_EXISTING_S32DS,
        InstallStatus.REQUIRES_SELECTION,
    }
)


def questions_for(
    result: InstallResult,
    *,
    installer_param: str = "installerPath",
    s32ds_candidates: tuple[str, ...] = (),
) -> tuple[InstallQuestion, ...]:
    """Derive the questions that would let ``result`` be retried successfully.

    ``installer_param`` differs between actions - ``tool.install`` takes
    ``installerPath`` while ``tool.install_from_artifact`` takes ``path`` - so
    the caller names it rather than the agent having to translate.
    """

    status = result.status

    if status is InstallStatus.REQUIRES_SOURCE:
        return (
            InstallQuestion(
                id="source",
                prompt=(
                    f"Download {result.component} from one of the locations listed in "
                    "'sources', then give the full path to the downloaded file."
                ),
                kind=QuestionKind.PATH,
                param=installer_param,
                why=(
                    "The artifact has to be downloaded by hand: the download pages "
                    "require signing in and accepting a licence."
                ),
            ),
        )

    if status is InstallStatus.REQUIRES_INSTALLER_FILE:
        return (
            InstallQuestion(
                id="installer_path",
                prompt=(
                    "The location given could not be used as an installer. Provide the "
                    "path to the installer executable, or to a folder containing exactly "
                    "one."
                ),
                kind=QuestionKind.PATH,
                param=installer_param,
                why=result.error or "The supplied location is not a usable installer.",
            ),
        )

    if status is InstallStatus.REQUIRES_EXISTING_S32DS:
        return (
            InstallQuestion(
                id="s32ds_path",
                prompt=(
                    "Which S32 Design Studio installation should this be installed into?"
                    if s32ds_candidates
                    else "Give the path of the S32 Design Studio installation to install "
                    "into, for example C:/NXP/S32DS.3.6.4."
                ),
                kind=QuestionKind.CHOICE if s32ds_candidates else QuestionKind.PATH,
                param="s32dsPath",
                choices=s32ds_candidates,
                default=s32ds_candidates[0] if len(s32ds_candidates) == 1 else "",
                why=(
                    "An update site is installed by an existing S32 Design Studio, so "
                    "one has to be named."
                ),
            ),
        )

    if status is InstallStatus.REQUIRES_SELECTION:
        return (
            InstallQuestion(
                id="iu_ids",
                prompt=(
                    "This update site publishes several products. Which ones should be "
                    "installed?"
                ),
                kind=QuestionKind.MULTI_CHOICE,
                param="iuIds",
                choices=result.candidates,
                why=(
                    "Selection was requested, so nothing was installed. Without it the "
                    "whole site is installed."
                ),
            ),
        )

    return ()


def next_step_for(result: InstallResult, *, action_name: str = "") -> str:
    """One sentence telling the agent what to do with the answers."""

    if result.status not in _ACTIONABLE:
        return ""
    target = action_name or "the same action"
    return (
        f"Ask the questions below, then call {target} again with the answers filled "
        "into the named parameters. No state is kept between calls, so include "
        "everything that was already supplied."
    )


def with_guidance(
    result: InstallResult,
    *,
    action_name: str = "",
    installer_param: str = "installerPath",
    s32ds_candidates: tuple[str, ...] = (),
    sources: tuple[SourceCandidate, ...] | None = None,
) -> InstallResult:
    """Attach questions, a next step, and download locations to ``result``.

    A terminal result is returned unchanged, so this can be applied
    unconditionally on the way out of a provider.
    """

    if result.status not in _ACTIONABLE:
        return result

    candidates = sources if sources is not None else candidates_for(result.component)
    return dataclasses.replace(
        result,
        questions=questions_for(
            result,
            installer_param=installer_param,
            s32ds_candidates=s32ds_candidates,
        ),
        next_step=next_step_for(result, action_name=action_name),
        # Only worth listing when the problem is actually "where do I get this".
        sources=(
            candidates
            if result.status
            in (InstallStatus.REQUIRES_SOURCE, InstallStatus.REQUIRES_INSTALLER_FILE)
            else result.sources
        ),
    )
