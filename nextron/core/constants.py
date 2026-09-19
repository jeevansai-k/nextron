"""Immutable project-wide constants for NEXTRON.

Nothing in this module may be modified at runtime. Every other module imports
its identity, branding and default port values from here so the project has a
single source of truth.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

APP_NAME: Final[str] = "NEXTRON"
APP_SLUG: Final[str] = "nextron"
VERSION: Final[str] = "1.0.0"
TAGLINE: Final[str] = "Intelligent Tor Switcher with VPN & Ad Blocking in CLI"
LICENSE: Final[str] = "MIT"

DEVELOPER: Final[str] = "Jeevan Sai K"
GITHUB_USER: Final[str] = "jeevansai-k"
GITHUB_URL: Final[str] = "https://github.com/jeevansai-k"
EMAIL: Final[str] = "kukkalajeevansai@gmail.com"

# --------------------------------------------------------------------------- #
# Brand colour hierarchy -- the exclusive palette of the project
# --------------------------------------------------------------------------- #

COLOR_PRIMARY: Final[str] = "#4B0082"
COLOR_SECONDARY: Final[str] = "#5D3D94"
COLOR_ACCENT: Final[str] = "#9B59B6"
COLOR_SURFACE: Final[str] = "#C8A2C8"
COLOR_TEXT: Final[str] = "#E6E6FA"

PALETTE: Final[dict[str, str]] = {
    "primary": COLOR_PRIMARY,
    "secondary": COLOR_SECONDARY,
    "accent": COLOR_ACCENT,
    "surface": COLOR_SURFACE,
    "text": COLOR_TEXT,
}

# --------------------------------------------------------------------------- #
# Networking defaults
# --------------------------------------------------------------------------- #

TOR_SOCKS_PORT: Final[int] = 9050
TOR_CONTROL_PORT: Final[int] = 9051
TOR_DNS_PORT: Final[int] = 9053
TOR_TRANS_PORT: Final[int] = 9040

DNS_SHIELD_PORT: Final[int] = 53
DNS_SHIELD_FALLBACK_PORT: Final[int] = 5353

# --------------------------------------------------------------------------- #
# Rotation bounds (seconds) -- enforced everywhere, spec mandated
# --------------------------------------------------------------------------- #

ROTATION_MIN_SECONDS: Final[int] = 5
ROTATION_MAX_SECONDS: Final[int] = 300

#: The interval steps offered by the interface: 15s, 30s, then every 30s up to
#: 5 minutes. A hand-edited config.toml may still use any value in the
#: 5s-300s window above; these are the values the steppers walk through.
ROTATION_PRESETS: Final[tuple[int, ...]] = (
    15,
    30,
    *range(60, ROTATION_MAX_SECONDS + 1, 30),
)

# --------------------------------------------------------------------------- #
# Supported import formats
# --------------------------------------------------------------------------- #

VPN_IMPORT_SUFFIXES: Final[tuple[str, ...]] = (".ovpn", ".conf", ".wgconf", ".json")
BLOCKLIST_SUFFIXES: Final[tuple[str, ...]] = (".txt", ".hosts", ".list")

# --------------------------------------------------------------------------- #
# Public IP / verification endpoints (plain HTTP APIs, no accounts, no keys)
# --------------------------------------------------------------------------- #

IP_LOOKUP_ENDPOINTS: Final[tuple[str, ...]] = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.co/json",
    "https://ipinfo.io/json",
)
GEO_LOOKUP_ENDPOINT: Final[str] = "https://ipinfo.io/{ip}/json"
TOR_CHECK_ENDPOINT: Final[str] = "https://check.torproject.org/api/ip"
IPV6_PROBE_ENDPOINT: Final[str] = "https://api6.ipify.org?format=json"

NETWORK_TIMEOUT: Final[float] = 15.0
