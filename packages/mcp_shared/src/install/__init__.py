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

"""Shared, non-interactive tool-provisioning layer.

All install mechanics live in :mod:`base`; a product package contributes a
provider by declaring class constants and ``offers()``, then registering it into
:data:`INSTALL_PROVIDERS`. See ``docs/TOOL_PROVISIONING.md``.

Two cross-cutting helpers sit alongside it: :mod:`sources_catalog` lists the
download pages a user can obtain an artifact from, and :mod:`guide` turns a
blocked result into the questions that would unblock it.
"""

# Detection lives in nxp.mcp.shared.detect; re-exported here because the
# install layer and its existing callers resolve S32DS installations through it.
from ..detect.scan import (
    default_scan_roots,
    iter_install_dirs,
    parse_s32ds_version,
)
from .action import (
    build_install_action,
    build_install_actions,
    build_install_from_artifact_action,
    build_install_offers_action,
)
from .base import (
    ALL_PLATFORMS,
    LINUX_ONLY,
    WINDOWS_ONLY,
    ExeInstallerProvider,
    GuidanceProvider,
    InstallProvider,
    P2UpdateSiteProvider,
    current_platform,
)
from .guide import (
    next_step_for,
    questions_for,
    with_guidance,
)
from .models import (
    InstallDelivery,
    InstalledSnapshot,
    InstallMethod,
    InstallMode,
    InstallOffer,
    InstallPlan,
    InstallQuestion,
    InstallRequest,
    InstallResult,
    InstallStatus,
    QuestionKind,
    SourceCandidate,
)
from .registry import (
    INSTALL_PROVIDERS,
    S32DS_COMPONENT,
    ArtifactMatch,
    InstallProviderRegistry,
    register_install_provider,
)
from .source import (
    ArtifactKind,
    FetchResult,
    InstallSource,
    ResolvedSource,
    SourceLookup,
    classify_artifact,
    fetch_to_temp,
    installer_suffix,
    is_remote,
    looks_like_update_site,
    normalise_repository,
    resolve_installer,
    resolve_source,
)
from .sources_catalog import (
    DEFAULT_SOURCES,
    P2_REQUIRES_S32DS_NOTE,
    PACKAGE_MANAGER,
    S32DS_PRODUCT_PAGE,
    SOFTWARE_CATALOG,
    SOURCE_CATALOG,
    candidates_for,
    catalog_urls_for,
    package_manager_url,
)

__all__ = [
    "ALL_PLATFORMS",
    "DEFAULT_SOURCES",
    "INSTALL_PROVIDERS",
    "LINUX_ONLY",
    "P2_REQUIRES_S32DS_NOTE",
    "PACKAGE_MANAGER",
    "S32DS_COMPONENT",
    "S32DS_PRODUCT_PAGE",
    "SOFTWARE_CATALOG",
    "SOURCE_CATALOG",
    "WINDOWS_ONLY",
    "ArtifactKind",
    "ArtifactMatch",
    "ExeInstallerProvider",
    "FetchResult",
    "GuidanceProvider",
    "InstallDelivery",
    "InstallMethod",
    "InstallMode",
    "InstallOffer",
    "InstallPlan",
    "InstallProvider",
    "InstallProviderRegistry",
    "InstallQuestion",
    "InstallRequest",
    "InstallResult",
    "InstallSource",
    "InstallStatus",
    "InstalledSnapshot",
    "P2UpdateSiteProvider",
    "QuestionKind",
    "ResolvedSource",
    "SourceCandidate",
    "SourceLookup",
    "build_install_action",
    "build_install_actions",
    "build_install_from_artifact_action",
    "build_install_offers_action",
    "candidates_for",
    "catalog_urls_for",
    "classify_artifact",
    "current_platform",
    "default_scan_roots",
    "fetch_to_temp",
    "installer_suffix",
    "is_remote",
    "iter_install_dirs",
    "looks_like_update_site",
    "next_step_for",
    "normalise_repository",
    "package_manager_url",
    "parse_s32ds_version",
    "questions_for",
    "register_install_provider",
    "resolve_installer",
    "resolve_source",
    "with_guidance",
]
