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

"""Registry and planning for the shared install layer.

Product packages register their providers explicitly at import time (the same
convention as the hand-written ``STATIC_ACTIONS`` tuples), so the set of known
providers is deterministic and greppable - there is no entry-point or
filesystem scanning.

The registry also owns the coordination logic that must not live in a skill:
which offer to prefer when a component is available both standalone and inside
S32 Design Studio, how to collapse several requested components into a single
installer run, and when a p2 install has to be preceded by installing S32
Design Studio itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..detect import S32DS_COMPONENT
from .base import InstallProvider
from .models import (
    InstallDelivery,
    InstallMethod,
    InstalledSnapshot,
    InstallOffer,
    InstallPlan,
)
from .source import ArtifactKind, ResolvedSource, classify_artifact

logger = logging.getLogger(__name__)


# Artifact kinds that can only be delivered by each method.
_EXE_KINDS = (ArtifactKind.EXE, ArtifactKind.INSTALLER_DIR)
_P2_KINDS = (ArtifactKind.P2_ZIP, ArtifactKind.P2_DIR)


@dataclass(frozen=True, slots=True)
class ArtifactMatch:
    """An artifact resolved to the provider that can install it."""

    source: ResolvedSource
    offer: InstallOffer | None = None
    provider: InstallProvider | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.offer is not None


class InstallProviderRegistry:
    """Mutable set of install providers keyed by provider name."""

    def __init__(self) -> None:
        self._providers: dict[str, InstallProvider] = {}

    def register(self, provider: InstallProvider, *, replace: bool = False) -> InstallProvider:
        name = provider.PROVIDER
        if name in self._providers and not replace:
            raise ValueError(f"Duplicate install provider '{name}'")
        self._providers[name] = provider
        return provider

    def get(self, provider: str) -> InstallProvider | None:
        return self._providers.get(provider)

    def list_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def list_offers(self) -> tuple[InstallOffer, ...]:
        offers: list[InstallOffer] = []
        for provider in self._providers.values():
            offers.extend(provider.offers())
        return tuple(offers)

    def components(self) -> tuple[str, ...]:
        return tuple(sorted({offer.component for offer in self.list_offers()}))

    def offers_of(
        self,
        component: str,
        *,
        installed: InstalledSnapshot | None = None,
    ) -> tuple[InstallOffer, ...]:
        """Return every offer for ``component``, best first.

        Ranking, in order of decreasing weight:

        1. An offer that installs into an S32 Design Studio that already exists
           wins - it is by far the cheapest action available.
        2. A p2 offer with no S32DS present is penalised, because taking it
           implies also installing S32 Design Studio.
        3. Otherwise the provider-declared ``rank`` decides, standalone before
           bundle on a tie.
        """

        matches = [offer for offer in self.list_offers() if offer.component == component]
        s32ds_present = bool((installed or {}).get(S32DS_COMPONENT))

        def sort_key(offer: InstallOffer) -> tuple[int, int, str]:
            if offer.method is InstallMethod.P2_UPDATE_SITE:
                situational = 0 if s32ds_present else 2
            elif offer.delivery is InstallDelivery.INTO_S32DS and s32ds_present:
                situational = 0
            else:
                situational = 1
            return (situational, offer.rank, offer.provider)

        return tuple(sorted(matches, key=sort_key))

    def best_offer(
        self,
        component: str,
        *,
        installed: InstalledSnapshot | None = None,
    ) -> InstallOffer | None:
        ranked = self.offers_of(component, installed=installed)
        return ranked[0] if ranked else None

    def match_artifact(
        self,
        location: str | Path,
        *,
        installed: InstalledSnapshot | None = None,
    ) -> ArtifactMatch:
        """Resolve a downloaded artifact to the offer that can install it.

        This is what makes "install from <path>" deterministic: the artifact's
        shape decides the method (an update-site zip can only be a p2 install,
        an executable can only be an exe install), and the filename decides the
        product. Nothing is inferred by the caller.
        """

        source = classify_artifact(location)
        if not source.ok:
            return ArtifactMatch(source=source, error=source.error)

        if source.kind in _P2_KINDS:
            methods = (InstallMethod.P2_UPDATE_SITE,)
        elif source.kind in _EXE_KINDS:
            methods = (InstallMethod.EXE_INSTALLER,)
        else:
            return ArtifactMatch(
                source=source,
                error=f"Cannot tell what kind of install artifact {location} is.",
            )

        filename = Path(str(source.path or location)).name
        candidates = [
            offer
            for offer in self.list_offers()
            if offer.method in methods
            and (provider := self._providers.get(offer.provider)) is not None
            and provider.recognises(filename)
        ]
        if not candidates:
            return ArtifactMatch(
                source=source,
                error=(
                    f"No registered provider recognises '{filename}'. Pass the "
                    "component explicitly, or register a provider for this product."
                ),
            )

        # Several offers of one provider can match (a bundle supplies many
        # components); prefer the offer whose own component is the product.
        s32ds_present = bool((installed or {}).get(S32DS_COMPONENT))
        candidates.sort(
            key=lambda offer: (
                offer.rank,
                0 if offer.delivery is InstallDelivery.STANDALONE or s32ds_present else 1,
                offer.component,
            )
        )
        chosen = candidates[0]
        return ArtifactMatch(
            source=source,
            offer=chosen,
            provider=self._providers.get(chosen.provider),
        )

    def plan(
        self,
        components: Iterable[str],
        *,
        installed: InstalledSnapshot | None = None,
    ) -> InstallPlan:
        """Build an ordered, de-duplicated plan for the requested components.

        Components already present are dropped. Components that another chosen
        offer already ``supplies`` are folded into that offer rather than
        installed twice. A p2 step with no host installation gets the S32 Design
        Studio install prepended.
        """

        requested = tuple(dict.fromkeys(components))
        snapshot = dict(installed or {})

        steps: list[InstallOffer] = []
        unresolved: list[str] = []
        rationale: list[str] = []
        satisfied: set[str] = set()

        for component in requested:
            if snapshot.get(component):
                rationale.append(f"{component}: already installed at {snapshot[component]}.")
                continue
            if component in satisfied:
                owner = next(step.provider for step in steps if component in step.supplies)
                rationale.append(f"{component}: covered by the {owner} step already planned.")
                continue

            offer = self.best_offer(component, installed=snapshot)
            if offer is None:
                unresolved.append(component)
                rationale.append(f"{component}: no provider is registered for it.")
                continue

            if offer.method is InstallMethod.P2_UPDATE_SITE and not snapshot.get(S32DS_COMPONENT):
                host = self.best_offer(S32DS_COMPONENT, installed=snapshot)
                if host is None:
                    unresolved.append(component)
                    rationale.append(
                        f"{component}: needs S32 Design Studio, but no provider for it "
                        "is registered."
                    )
                    continue
                if host not in steps:
                    steps.append(host)
                    satisfied |= {host.component, *host.supplies}
                    rationale.append(
                        f"{S32DS_COMPONENT}: installed first because {component} is "
                        "delivered through its update site."
                    )

            steps.append(offer)
            satisfied |= {offer.component, *offer.supplies}
            rationale.append(
                f"{component}: {offer.provider} via {offer.method} ({offer.delivery})."
            )

        blocking = sorted(
            {
                f"{step.provider}: an installer file must be supplied."
                for step in steps
                if step.requires_installer_file
            }
        )

        return InstallPlan(
            requested=requested,
            steps=tuple(steps),
            rationale=" ".join(rationale),
            blocking=tuple(blocking),
            unresolved=tuple(unresolved),
        )


# Process-wide registry every product package registers into.
INSTALL_PROVIDERS = InstallProviderRegistry()


def register_install_provider(
    provider_cls: type[InstallProvider],
) -> type[InstallProvider]:
    """Class decorator registering a provider into :data:`INSTALL_PROVIDERS`."""

    INSTALL_PROVIDERS.register(provider_cls())
    return provider_cls
