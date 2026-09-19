"""The VPN profile library.

Unlimited local profiles live under ``~/.config/nextron/profiles``: the
configuration file itself plus an ``index.json`` holding metadata. Four import
formats are supported -- ``.ovpn``, ``.conf``, ``.wgconf`` and ``.json`` -- and
the protocol is detected from the file's contents, not merely its extension.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

from nextron.core import constants
from nextron.core.exceptions import VPNProfileError
from nextron.storage import paths
from nextron.vpn.certs import certificate_expiry

log = logging.getLogger(__name__)

__all__ = [
    "ProfileLibrary",
    "VPNProfile",
    "VPNProtocol",
    "detect_protocol",
    "fingerprint",
    "wants_credentials",
]

_WG_MARKERS = ("[interface]", "privatekey", "[peer]", "endpoint =")
_OVPN_MARKERS = ("client", "remote ", "dev tun", "dev tap", "<ca>", "proto ")
_INDEX_NAME = "index.json"


class VPNProtocol(str, Enum):
    """The two supported VPN technologies."""

    OPENVPN = "openvpn"
    WIREGUARD = "wireguard"

    @property
    def label(self) -> str:
        return "OpenVPN" if self is VPNProtocol.OPENVPN else "WireGuard"

    @property
    def suffix(self) -> str:
        return ".ovpn" if self is VPNProtocol.OPENVPN else ".conf"


def detect_protocol(text: str, suffix: str = "") -> VPNProtocol:
    """Detect the protocol from configuration *text* (extension as a tiebreak)."""
    lowered = text.lower()
    wg_hits = sum(marker in lowered for marker in _WG_MARKERS)
    ovpn_hits = sum(marker in lowered for marker in _OVPN_MARKERS)

    if wg_hits and wg_hits >= ovpn_hits:
        return VPNProtocol.WIREGUARD
    if ovpn_hits:
        return VPNProtocol.OPENVPN

    suffix = suffix.lower()
    if suffix == ".wgconf":
        return VPNProtocol.WIREGUARD
    if suffix == ".ovpn":
        return VPNProtocol.OPENVPN
    raise VPNProfileError(
        "Cannot determine whether this is an OpenVPN or WireGuard profile"
    )


def fingerprint(text: str) -> str:
    """Identify a configuration by its contents, not its file name.

    Lets the ``Sources/`` importer recognise a renamed copy of a profile the
    library already holds.
    """
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


def _extract_endpoint(text: str, protocol: VPNProtocol) -> str | None:
    """Pull the server endpoint out of a configuration, for display and killswitch."""
    if protocol is VPNProtocol.WIREGUARD:
        match = re.search(r"^\s*Endpoint\s*=\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
        return match.group(1) if match else None
    match = re.search(
        r"^\s*remote\s+(\S+)(?:\s+(\d+))?", text, re.MULTILINE | re.IGNORECASE
    )
    if not match:
        return None
    host, port = match.group(1), match.group(2)
    return f"{host}:{port}" if port else host


def wants_credentials(text: str) -> bool:
    """True when a configuration asks OpenVPN for a username and password.

    ``auth-user-pass`` with no file argument makes OpenVPN query for them
    interactively. NEXTRON runs it with no console, so such a profile can only
    work once the user points it at a credentials file of their own.
    """
    return bool(
        re.search(r"^[ \t]*auth-user-pass[ \t]*(?:#.*)?$", text, re.MULTILINE)
    )


def _uses_tcp(text: str) -> bool:
    """True when an OpenVPN profile is TCP based (required for VPN over Tor)."""
    return bool(
        re.search(
            r"^\s*(proto|remote\s+\S+\s+\d+)\s+tcp",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
    )


@dataclass(frozen=True, slots=True)
class VPNProfile:
    """One importable, activatable VPN profile."""

    id: str
    name: str
    protocol: VPNProtocol
    filename: str
    added_at: datetime
    favorite: bool = False
    endpoint: str | None = None
    tcp: bool = False
    auth_file: str | None = None
    last_used: datetime | None = None
    use_count: int = 0
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    #: When the profile's inline client certificate expires, if it has one.
    expires_at: datetime | None = None

    @property
    def path(self) -> Path:
        return paths.profiles_dir() / self.filename

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def display(self) -> str:
        star = "✦ " if self.favorite else ""
        return f"{star}{self.name}"

    @property
    def expired(self) -> bool:
        """True when the embedded client certificate is past its end date."""
        from nextron.vpn.certs import is_expired

        return is_expired(self.expires_at)

    @property
    def expiry(self) -> str:
        """``"in 13d"``, ``"EXPIRED"`` or ``"--"``."""
        from nextron.vpn.certs import expiry_label

        return expiry_label(self.expires_at)

    @property
    def requires_credentials(self) -> bool:
        """True when this profile needs a username and password it has not got.

        Read from the configuration rather than stored, so a profile stays
        correct whatever version of NEXTRON wrote the library index.
        """
        if self.auth_file:
            return False
        try:
            return wants_credentials(self.read_config())
        except VPNProfileError:
            return False

    @property
    def supports_socks(self) -> bool:
        """Only TCP OpenVPN can be tunnelled through Tor's SOCKS proxy."""
        return self.protocol is VPNProtocol.OPENVPN and self.tcp

    def read_config(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise VPNProfileError(f"Cannot read profile '{self.name}': {exc}") from exc

    # -- (de)serialisation -------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol.value,
            "filename": self.filename,
            "added_at": self.added_at.isoformat(timespec="seconds"),
            "favorite": self.favorite,
            "endpoint": self.endpoint,
            "tcp": self.tcp,
            "auth_file": self.auth_file,
            "last_used": self.last_used.isoformat(timespec="seconds")
            if self.last_used
            else None,
            "use_count": self.use_count,
            "notes": self.notes,
            "tags": list(self.tags),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> VPNProfile:
        def _moment(key: str) -> datetime | None:
            raw = data.get(key)
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                return None

        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            protocol=VPNProtocol(data.get("protocol", "openvpn")),
            filename=str(data["filename"]),
            added_at=_moment("added_at") or datetime.now(),
            favorite=bool(data.get("favorite", False)),
            endpoint=data.get("endpoint"),
            tcp=bool(data.get("tcp", False)),
            auth_file=data.get("auth_file"),
            last_used=_moment("last_used"),
            use_count=int(data.get("use_count", 0) or 0),
            notes=str(data.get("notes", "")),
            tags=tuple(data.get("tags", ()) or ()),
            expires_at=_moment("expires_at"),
        )


class ProfileLibrary:
    """Import, organise and persist an unlimited number of VPN profiles."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or paths.profiles_dir()
        self._profiles: dict[str, VPNProfile] = {}

    # -- properties --------------------------------------------------------- #

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def index_path(self) -> Path:
        return self._dir / _INDEX_NAME

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self):
        return iter(self.all())

    # -- persistence -------------------------------------------------------- #

    def load(self) -> list[VPNProfile]:
        """Load the index, dropping entries whose file has disappeared."""
        paths.ensure_layout()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._profiles.clear()

        if self.index_path.is_file():
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Profile index unreadable (%s); rebuilding", exc)
                raw = []
            for entry in raw if isinstance(raw, list) else []:
                try:
                    profile = VPNProfile.from_dict(entry)
                except (KeyError, ValueError) as exc:
                    log.warning("Skipping malformed profile entry: %s", exc)
                    continue
                if profile.exists:
                    self._profiles[profile.id] = profile
                else:
                    log.warning(
                        "Profile '%s' dropped: %s is missing", profile.name, profile.path
                    )

        self._adopt_orphans()
        self.save()
        return self.all()

    def _adopt_orphans(self) -> None:
        """Pick up configuration files dropped straight into ``profiles/``."""
        known = {profile.filename for profile in self._profiles.values()}
        for candidate in sorted(self._dir.iterdir()):
            if (
                not candidate.is_file()
                or candidate.name == _INDEX_NAME
                or candidate.name in known
                or candidate.suffix.lower() not in constants.VPN_IMPORT_SUFFIXES
            ):
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
                protocol = detect_protocol(text, candidate.suffix)
            except (OSError, VPNProfileError) as exc:
                log.debug("Ignoring %s (%s)", candidate.name, exc)
                continue

            profile = VPNProfile(
                id=self._new_id(),
                name=candidate.stem,
                protocol=protocol,
                filename=candidate.name,
                added_at=datetime.now(),
                endpoint=_extract_endpoint(text, protocol),
                tcp=_uses_tcp(text),
                expires_at=certificate_expiry(text),
            )
            self._profiles[profile.id] = profile
            log.info("Adopted profile '%s' found in the profiles directory", profile.name)

    def save(self) -> None:
        payload = [profile.to_dict() for profile in self.all()]
        try:
            self.index_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise VPNProfileError(f"Cannot write the profile index: {exc}") from exc

    # -- queries ------------------------------------------------------------ #

    def all(self) -> list[VPNProfile]:
        """Every profile, favourites first, then alphabetical."""
        return sorted(
            self._profiles.values(), key=lambda p: (not p.favorite, p.name.lower())
        )

    def get(self, profile_id: str | None) -> VPNProfile | None:
        if not profile_id:
            return None
        return self._profiles.get(profile_id)

    def by_name(self, name: str) -> VPNProfile | None:
        lowered = name.strip().lower()
        for profile in self._profiles.values():
            if profile.name.lower() == lowered:
                return profile
        return None

    def resolve(self, needle: str) -> VPNProfile | None:
        """Look a profile up by id, exact name, or unique name prefix."""
        return (
            self.get(needle)
            or self.by_name(needle)
            or next(
                (
                    p
                    for p in self.all()
                    if p.name.lower().startswith(needle.strip().lower())
                ),
                None,
            )
        )

    def favorites(self) -> list[VPNProfile]:
        return [profile for profile in self.all() if profile.favorite]

    def pool(self, profile_ids: list[str]) -> list[VPNProfile]:
        """Resolve a rotation pool, falling back to every profile when empty."""
        chosen = [self.get(pid) for pid in profile_ids]
        resolved = [profile for profile in chosen if profile is not None]
        return resolved or self.all()

    # -- mutations ---------------------------------------------------------- #

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def import_file(self, source: Path, *, name: str | None = None) -> VPNProfile:
        """Copy *source* into the library and register it."""
        source = Path(source).expanduser()
        if not source.is_file():
            raise VPNProfileError(f"No such file: {source}")
        if source.suffix.lower() not in constants.VPN_IMPORT_SUFFIXES:
            supported = ", ".join(constants.VPN_IMPORT_SUFFIXES)
            raise VPNProfileError(
                f"Unsupported format '{source.suffix}'. Supported: {supported}"
            )

        if source.suffix.lower() == ".json":
            return self._import_descriptor(source, name=name)

        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise VPNProfileError(f"Cannot read {source}: {exc}") from exc

        protocol = detect_protocol(text, source.suffix)
        return self._store(
            name=name or source.stem,
            protocol=protocol,
            text=text,
            original_suffix=source.suffix,
        )

    def _import_descriptor(self, source: Path, *, name: str | None) -> VPNProfile:
        """Import a NEXTRON ``.json`` descriptor.

        Shape::

            {"name": "...", "protocol": "openvpn|wireguard",
             "config": "<inline text>"  |  "config_path": "/path/to/file",
             "auth_file": "/path/to/credentials"}
        """
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VPNProfileError(f"Invalid JSON profile: {exc}") from exc
        if not isinstance(data, dict):
            raise VPNProfileError("A JSON profile must be an object")

        text = data.get("config")
        if not text and data.get("config_path"):
            nested = Path(str(data["config_path"])).expanduser()
            if not nested.is_file():
                raise VPNProfileError(f"config_path does not exist: {nested}")
            text = nested.read_text(encoding="utf-8", errors="replace")
        if not text:
            raise VPNProfileError("JSON profile has neither 'config' nor 'config_path'")

        declared = data.get("protocol")
        protocol = (
            VPNProtocol(declared)
            if declared in {p.value for p in VPNProtocol}
            else detect_protocol(text)
        )
        return self._store(
            name=name or str(data.get("name") or source.stem),
            protocol=protocol,
            text=text,
            original_suffix=protocol.suffix,
            auth_file=data.get("auth_file"),
        )

    def _store(
        self,
        *,
        name: str,
        protocol: VPNProtocol,
        text: str,
        original_suffix: str,
        auth_file: str | None = None,
    ) -> VPNProfile:
        endpoint = _extract_endpoint(text, protocol)
        if endpoint is None:
            expected = (
                "an 'Endpoint =' line"
                if protocol is VPNProtocol.WIREGUARD
                else "a 'remote' line"
            )
            raise VPNProfileError(
                f"Not a usable {protocol.label} configuration: no server address "
                f"({expected} is missing)"
            )

        paths.ensure_layout()
        profile_id = self._new_id()
        suffix = ".conf" if protocol is VPNProtocol.WIREGUARD else ".ovpn"
        filename = f"{_slug(name)}-{profile_id}{suffix}"
        target = self._dir / filename

        try:
            target.write_text(text, encoding="utf-8")
            target.chmod(0o600)
        except OSError as exc:
            raise VPNProfileError(f"Cannot store profile: {exc}") from exc

        profile = VPNProfile(
            id=profile_id,
            name=_unique_name(name, {p.name for p in self._profiles.values()}),
            protocol=protocol,
            filename=filename,
            added_at=datetime.now(),
            endpoint=endpoint,
            tcp=_uses_tcp(text),
            auth_file=auth_file,
            expires_at=certificate_expiry(text),
        )
        self._profiles[profile.id] = profile
        self.save()
        log.info("Imported %s profile '%s'", protocol.label, profile.name)
        return profile

    def rename(self, profile_id: str, new_name: str) -> VPNProfile:
        profile = self._require(profile_id)
        clean = new_name.strip()
        if not clean:
            raise VPNProfileError("A profile name cannot be empty")
        updated = replace(profile, name=clean)
        self._profiles[profile_id] = updated
        self.save()
        return updated

    def delete(self, profile_id: str) -> None:
        profile = self._require(profile_id)
        try:
            profile.path.unlink(missing_ok=True)
        except OSError as exc:
            raise VPNProfileError(f"Cannot delete {profile.path}: {exc}") from exc
        del self._profiles[profile_id]
        self.save()
        log.info("Deleted profile '%s'", profile.name)

    def toggle_favorite(self, profile_id: str) -> VPNProfile:
        profile = self._require(profile_id)
        updated = replace(profile, favorite=not profile.favorite)
        self._profiles[profile_id] = updated
        self.save()
        return updated

    def mark_used(self, profile_id: str) -> VPNProfile:
        profile = self._require(profile_id)
        updated = replace(
            profile, last_used=datetime.now(), use_count=profile.use_count + 1
        )
        self._profiles[profile_id] = updated
        self.save()
        return updated

    def set_auth_file(self, profile_id: str, auth_file: str | None) -> VPNProfile:
        """Point an OpenVPN profile at a credentials file the user supplied."""
        profile = self._require(profile_id)
        if auth_file:
            candidate = Path(auth_file).expanduser()
            if not candidate.is_file():
                raise VPNProfileError(f"Credentials file not found: {candidate}")
            auth_file = str(candidate)
        updated = replace(profile, auth_file=auth_file)
        self._profiles[profile_id] = updated
        self.save()
        return updated

    def export(self, profile_id: str, destination: Path) -> Path:
        profile = self._require(profile_id)
        destination = Path(destination).expanduser()
        if destination.is_dir():
            destination = destination / profile.filename
        try:
            shutil.copy2(profile.path, destination)
        except OSError as exc:
            raise VPNProfileError(f"Export failed: {exc}") from exc
        return destination

    def _require(self, profile_id: str) -> VPNProfile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise VPNProfileError(f"Unknown profile id: {profile_id}")
        return profile


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return (cleaned or "profile")[:48].lower()


def _unique_name(name: str, existing: set[str]) -> str:
    candidate = name.strip() or "Profile"
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{candidate} ({counter})" in existing:
        counter += 1
    return f"{candidate} ({counter})"
