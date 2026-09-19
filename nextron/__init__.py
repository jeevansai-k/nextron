"""NEXTRON -- Intelligent Tor Switcher with VPN & Ad Blocking in CLI.

A 100% terminal-native, keyboard-first privacy router: four routing modes
(Tor, VPN, Tor over VPN, VPN over Tor), two independent rotation schedulers,
an ad/tracker/malware DNS Shield, a kill switch and a verification engine that
refuses to report a connection until every check passes.

Local-first, zero accounts, zero telemetry, MIT licensed.
"""

from nextron.core.constants import (
    APP_NAME,
    DEVELOPER,
    EMAIL,
    GITHUB_URL,
    LICENSE,
    TAGLINE,
    VERSION,
)

__all__ = [
    "APP_NAME",
    "DEVELOPER",
    "EMAIL",
    "GITHUB_URL",
    "LICENSE",
    "TAGLINE",
    "VERSION",
    "__version__",
]

__version__ = VERSION
