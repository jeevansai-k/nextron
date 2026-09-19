"""Best-effort inspection of the certificates embedded in a VPN profile.

Many ``.ovpn`` files carry an inline client certificate with a fixed lifetime.
When it expires the tunnel fails with a TLS handshake error that says nothing
useful, so NEXTRON reads the expiry at import time and shows it in the library.

This is *metadata only*: it never gates a connection, and it degrades to
``None`` when the ``openssl`` binary is unavailable. No cryptography dependency
is added for a cosmetic field.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "certificate_expiry",
    "expires_within",
    "expiry_label",
    "extract_client_certificate",
    "is_expired",
]

_CERT_BLOCK_RE = re.compile(r"<cert>\s*(.*?)\s*</cert>", re.DOTALL | re.IGNORECASE)
_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_END_DATE_RE = re.compile(r"notAfter=(.+)")
_OPENSSL_TIMEOUT = 5.0


def extract_client_certificate(config_text: str) -> str | None:
    """Return the PEM of the inline ``<cert>`` block, if the profile has one."""
    block = _CERT_BLOCK_RE.search(config_text)
    if block is None:
        return None
    pem = _PEM_RE.search(block.group(1))
    return pem.group(0) if pem else None


def certificate_expiry(config_text: str) -> datetime | None:
    """Return when the profile's client certificate expires (UTC), or ``None``."""
    pem = extract_client_certificate(config_text)
    if pem is None:
        return None

    openssl = shutil.which("openssl")
    if openssl is None:
        log.debug("openssl unavailable; certificate expiry not read")
        return None

    handle = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".pem", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(pem + "\n")
            temp_path = Path(handle.name)
    except OSError as exc:  # pragma: no cover - read-only temp dir
        log.debug("Cannot write a temporary certificate: %s", exc)
        return None

    try:
        completed = subprocess.run(
            [openssl, "x509", "-noout", "-enddate", "-in", str(temp_path)],
            capture_output=True,
            text=True,
            timeout=_OPENSSL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("openssl failed: %s", exc)
        return None
    finally:
        temp_path.unlink(missing_ok=True)

    match = _END_DATE_RE.search(completed.stdout or "")
    if match is None:
        return None

    raw = match.group(1).strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    log.debug("Unrecognised certificate date: %r", raw)
    return None


def expiry_label(expires_at: datetime | None, *, now: datetime | None = None) -> str:
    """``"in 13 days"``, ``"EXPIRED"``, or ``"--"`` when unknown."""
    if expires_at is None:
        return "--"
    moment = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    remaining = (expires_at - moment).total_seconds()
    if remaining <= 0:
        return "EXPIRED"
    days = int(remaining // 86_400)
    if days >= 365:
        return f"in {days // 365}y"
    if days >= 1:
        return f"in {days}d"
    return f"in {int(remaining // 3600)}h"


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    moment = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= moment


def expires_within(
    expires_at: datetime | None, days: int, *, now: datetime | None = None
) -> bool:
    """True when the certificate expires inside *days* (and has not already)."""
    if expires_at is None or is_expired(expires_at, now=now):
        return False
    moment = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return (expires_at - moment).total_seconds() <= days * 86_400
