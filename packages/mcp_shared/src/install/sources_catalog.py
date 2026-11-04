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

"""Where a user obtains an install artifact from.

The install layer never downloads on the user's behalf: product installers sit
behind interactive licence acceptance and sign-in, and an update-site zip is
often only reachable from a corporate mirror. So a component resolves to a
*place to download from*, the user fetches the artifact, and the local path
comes back in the next call.

The locations, in the order to present them in:

1. the Automotive Software Package Manager, which serves bundles - sets of
   components whose versions are known to work together. This is the right
   answer whenever it is not already clear which individual components are
   needed, because picking components one by one risks an incompatible mix.
2. the software catalog, which serves one component at a time, for when the
   exact product and version are already known.

On top of that pair a component may declare its own product page - see
``SOURCE_CATALOG`` - which is offered after the Package Manager but before the
generic catalog, because it is the most direct route to that one product.
"""

from __future__ import annotations

from urllib.parse import quote

from .models import SourceCandidate

PACKAGE_MANAGER_URL = (
    "https://www.nxp.com/app-autopackagemgr/automotive-software-package-manager"
    ":AUTO-SW-PACKAGE-MANAGER"
)

SOFTWARE_CATALOG_URL = "https://www.nxp.com/design/design-center/software/automotive-software:ATOSFTWR"

S32DS_PRODUCT_PAGE_URL = (
    "https://www.nxp.com/design/software/development-software/s32-design-studio-ide"
)

PACKAGE_MANAGER = SourceCandidate(
    url=PACKAGE_MANAGER_URL,
    label="NXP Automotive Software Package Manager",
    origin="official",
    kind="package_manager",
    notes=(
        "Serves bundles: a set of components with versions validated against each "
        "other - S32 Design Studio, RTD, communication stacks, IPCF. Start here when "
        "it is not already clear which individual components are needed, or when "
        "several have to work together. The listing can be pre-filtered to one "
        "device family; see package_manager_url()."
    ),
    requires_auth=True,
)

SOFTWARE_CATALOG = SourceCandidate(
    url=SOFTWARE_CATALOG_URL,
    label="NXP software catalog",
    origin="official",
    kind="component_catalog",
    notes=(
        "Serves individual software components. Use when the exact product and "
        "version are already known; compatibility with the rest of the toolchain is "
        "then the user's responsibility."
    ),
    requires_auth=True,
)

S32DS_PRODUCT_PAGE = SourceCandidate(
    url=S32DS_PRODUCT_PAGE_URL,
    label="S32 Design Studio IDE product page",
    origin="official",
    kind="product_page",
    notes=(
        "The S32 Design Studio downloads, one version at a time. Use when the "
        "wanted version is already known and no bundle is needed; a Package "
        "Manager bundle is the better answer when other components have to match."
    ),
    requires_auth=True,
)

# Presented in this order for every component: bundles before single components.
DEFAULT_SOURCES: tuple[SourceCandidate, ...] = (PACKAGE_MANAGER, SOFTWARE_CATALOG)

# An Eclipse p2 component is delivered into a Design Studio, so it cannot be
# installed before one exists. Stated here because it changes what the user has
# to download: Design Studio first, then the update site.
P2_REQUIRES_S32DS_NOTE = (
    "Components delivered as an Eclipse update site are installed by an existing S32 "
    "Design Studio. With no Design Studio on the machine, install that first - a "
    "Package Manager bundle covers both in one download."
)

# Per-component overrides, for a product with a download page of its own. The
# generic pair still applies; the product page is inserted between them.
SOURCE_CATALOG: dict[str, tuple[SourceCandidate, ...]] = {
    "s32ds": (PACKAGE_MANAGER, S32DS_PRODUCT_PAGE, SOFTWARE_CATALOG),
}


def package_manager_url(device: str = "") -> str:
    """Package Manager URL, optionally pre-filtered to one device family.

    ``device`` is a family as the site labels it, e.g. "S32K3". An unknown value
    yields a page with an empty filter rather than an error, so it is never
    worth validating here.
    """

    if not device:
        return PACKAGE_MANAGER_URL
    return f"{PACKAGE_MANAGER_URL}?currentTab=0&selectedDevices={quote(device)}"


def candidates_for(component: str) -> tuple[SourceCandidate, ...]:
    """Return the download locations for ``component``, best first.

    Components without an entry in ``SOURCE_CATALOG`` resolve to the generic
    pair, so an unknown component is still answered.
    """

    return SOURCE_CATALOG.get(component) or DEFAULT_SOURCES


def catalog_urls_for(component: str) -> tuple[str, ...]:
    """Bare URLs for ``component``, for the plain ``suggested_sources`` field."""

    return tuple(candidate.url for candidate in candidates_for(component) if candidate.url)
