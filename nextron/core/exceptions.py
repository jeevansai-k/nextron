"""Exception hierarchy for NEXTRON.

Every engine raises a subclass of :class:`NextronError`, which lets the TUI and
the CLI present a single, predictable failure surface to the user.
"""

from __future__ import annotations


class NextronError(Exception):
    """Base class for every error raised inside NEXTRON."""

    #: Short, human readable label shown in the activity log.
    label: str = "Error"


class ConfigError(NextronError):
    """Raised when configuration on disk is invalid or cannot be written."""

    label = "Configuration"


class StorageError(NextronError):
    """Raised on database or filesystem failures inside the storage layer."""

    label = "Storage"


class PrivilegeError(NextronError):
    """Raised when an operation requires root and it is unavailable."""

    label = "Privilege"


class DependencyError(NextronError):
    """Raised when a required external binary is missing."""

    label = "Dependency"


class TorError(NextronError):
    """Raised by the Tor engine."""

    label = "Tor"


class TorBootstrapError(TorError):
    """Raised when the Tor daemon fails to reach 100% bootstrap in time."""


class TorControlError(TorError):
    """Raised when the Stem ControlPort connection fails or is rejected."""


class VPNError(NextronError):
    """Raised by the VPN engine."""

    label = "VPN"


class VPNProfileError(VPNError):
    """Raised when a VPN profile is malformed, unreadable or unsupported."""


class VPNConnectionError(VPNError):
    """Raised when a tunnel cannot be established or verified."""


class VPNCredentialsRequired(VPNConnectionError):
    """Raised when a profile asks for a username and password NEXTRON has not got.

    Distinct from a general connection failure because it is deterministic:
    retrying cannot help, and OpenVPN would otherwise sit waiting for an answer
    nobody can give it.
    """


class DNSError(NextronError):
    """Raised by the DNS Shield engine."""

    label = "DNS"


class RoutingError(NextronError):
    """Raised when a routing mode cannot be established or torn down."""

    label = "Routing"


class VerificationError(NextronError):
    """Raised when the verification engine rejects a connection."""

    label = "Verification"
