"""Descriptions of the four routing modes.

Kept separate from the manager so the TUI can render the execution sequence of
a mode before the user commits to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from nextron.core.config import RoutingMode

__all__ = ["MODE_DESCRIPTORS", "ModeDescriptor", "describe"]


@dataclass(frozen=True, slots=True)
class ModeDescriptor:
    """Everything the interface needs to present one routing mode."""

    mode: RoutingMode
    chain: str
    summary: str
    sequence: tuple[str, ...]
    requires_vpn_profile: bool
    notes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.mode.label


MODE_DESCRIPTORS: dict[RoutingMode, ModeDescriptor] = {
    RoutingMode.TOR_ONLY: ModeDescriptor(
        mode=RoutingMode.TOR_ONLY,
        chain=RoutingMode.TOR_ONLY.chain,
        summary="Every connection leaves through the Tor network, with "
        "automatic identity rotation.",
        sequence=(
            "Launch the Tor daemon",
            "Wait for a 100% bootstrap",
            "Engage transparent routing (or expose the SOCKS proxy)",
            "Verify the exit address with check.torproject.org",
            "Start the Tor rotation scheduler",
        ),
        requires_vpn_profile=False,
        notes=(
            "System-wide redirection needs root and the packaged Tor daemon.",
            "Without root, point applications at the SOCKS proxy on 127.0.0.1.",
        ),
    ),
    RoutingMode.VPN_ONLY: ModeDescriptor(
        mode=RoutingMode.VPN_ONLY,
        chain=RoutingMode.VPN_ONLY.chain,
        summary="A single VPN tunnel carries all traffic, with optional "
        "profile shuffling.",
        sequence=(
            "Arm the kill switch",
            "Connect the selected profile (OpenVPN or WireGuard)",
            "Confirm the tunnel interface and address",
            "Verify the public address changed",
            "Start the VPN shuffle scheduler",
        ),
        requires_vpn_profile=True,
        notes=("OpenVPN and WireGuard both require root to create a tunnel.",),
    ),
    RoutingMode.TOR_OVER_VPN: ModeDescriptor(
        mode=RoutingMode.TOR_OVER_VPN,
        chain=RoutingMode.TOR_OVER_VPN.chain,
        summary="The VPN hides Tor usage from the local network, then Tor "
        "anonymises the destination.",
        sequence=(
            "Connect the VPN",
            "Verify the tunnel",
            "Launch Tor (its traffic now rides the tunnel)",
            "Route Tor through the VPN",
            "Begin the Tor scheduler",
        ),
        requires_vpn_profile=True,
        notes=(
            "Both schedulers can run: Tor rotates identities, the VPN shuffles "
            "profiles independently.",
        ),
    ),
    RoutingMode.VPN_OVER_TOR: ModeDescriptor(
        mode=RoutingMode.VPN_OVER_TOR,
        chain=RoutingMode.VPN_OVER_TOR.chain,
        summary="Tor hides the VPN account from the provider; the exit is the "
        "VPN, not a Tor relay.",
        sequence=(
            "Launch Tor",
            "Wait for bootstrap",
            "Generate a temporary VPN runtime configuration",
            "Inject the SOCKS proxy",
            "Connect the VPN through Tor",
            "Verify the tunnel",
        ),
        requires_vpn_profile=True,
        notes=(
            "Requires a TCP OpenVPN profile: WireGuard is UDP-only and cannot "
            "traverse a SOCKS proxy.",
            "The kill switch stays off in this mode because Tor itself needs "
            "direct egress to reach guard relays.",
        ),
    ),
}


def describe(mode: RoutingMode) -> ModeDescriptor:
    return MODE_DESCRIPTORS[mode]
