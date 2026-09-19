"""Filesystem layout for NEXTRON.

Everything NEXTRON persists lives under ``~/.config/nextron`` and never leaves
the user's device::

    ~/.config/nextron/
        config.toml
        profiles/
        dns/
        stats.db
        logs/
        whitelist.txt
        runtime/        (generated torrc, temporary VPN configs, pid files)
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

__all__ = [
    "assets_dir",
    "banner_ascii",
    "banner_png",
    "config_file",
    "config_root",
    "database_file",
    "dns_dir",
    "dns_sources_dir",
    "ensure_layout",
    "logs_dir",
    "profiles_dir",
    "runtime_dir",
    "sources_dir",
    "vpn_sources_dir",
    "whitelist_file",
]


def invoking_user() -> tuple[int, int] | None:
    """``(uid, gid)`` of the user who ran ``sudo``, when that is not us."""
    name = os.environ.get("SUDO_USER")
    if not name or os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def _home() -> Path:
    """The home directory whose configuration NEXTRON should use.

    Under ``sudo`` that is the *invoking* user's home, not root's: the profiles
    and blocklists belong to the person who ran the command, and silently
    starting from an empty /root/.config would look like the data vanished.
    """
    name = os.environ.get("SUDO_USER")
    if name and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            pass
    return Path.home()


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    # Ignore XDG_CONFIG_HOME under sudo: it usually still points at root's.
    if raw and invoking_user() is None:
        return Path(raw).expanduser()
    return _home() / ".config"


def config_root() -> Path:
    """Return the NEXTRON configuration root, honouring ``NEXTRON_HOME``."""
    override = os.environ.get("NEXTRON_HOME")
    if override:
        return Path(override).expanduser()
    return _xdg_config_home() / "nextron"


def config_file() -> Path:
    return config_root() / "config.toml"


def profiles_dir() -> Path:
    return config_root() / "profiles"


def sources_dir() -> Path:
    """Drop-box the user manages with an ordinary file manager."""
    override = os.environ.get("NEXTRON_SOURCES")
    if override:
        return Path(override).expanduser()
    return config_root() / "Sources"


def vpn_sources_dir() -> Path:
    return sources_dir() / "VPN Profiles"


def dns_sources_dir() -> Path:
    return sources_dir() / "DNS list"


def dns_dir() -> Path:
    return config_root() / "dns"


def logs_dir() -> Path:
    return config_root() / "logs"


def runtime_dir() -> Path:
    return config_root() / "runtime"


def tor_data_dir() -> Path:
    return runtime_dir() / "tor"


def database_file() -> Path:
    return config_root() / "stats.db"


def whitelist_file() -> Path:
    return config_root() / "whitelist.txt"


def assets_dir() -> Path:
    """Locate the asset directory.

    Three layouts are supported, in order: an explicit ``NEXTRON_ASSETS``
    override, the copy bundled inside an installed wheel
    (``nextron/_assets``), and the editable repository layout (``assets/``
    next to the package).
    """
    override = os.environ.get("NEXTRON_ASSETS")
    if override:
        return Path(override).expanduser()

    package_dir = Path(__file__).resolve().parent.parent
    bundled = package_dir / "_assets"
    if bundled.is_dir():
        return bundled
    return package_dir.parent / "assets"


def banner_png() -> Path:
    return assets_dir() / "banner.png"


def banner_ascii() -> Path:
    return assets_dir() / "banner_ascii.txt"


def ensure_layout() -> Path:
    """Create the full directory layout if missing and return the root."""
    root = config_root()
    for directory in (
        root,
        profiles_dir(),
        dns_dir(),
        logs_dir(),
        runtime_dir(),
        tor_data_dir(),
        sources_dir(),
        vpn_sources_dir(),
        dns_sources_dir(),
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _write_sources_readme()

    # The Tor daemon refuses to start on a world readable DataDirectory.
    try:
        tor_data_dir().chmod(0o700)
    except OSError:  # pragma: no cover - exotic filesystems
        pass

    wl = whitelist_file()
    if not wl.exists():
        wl.write_text(
            "# NEXTRON DNS Shield whitelist\n"
            "# One domain per line. Whitelisted names are never blocked.\n",
            encoding="utf-8",
        )

    restore_ownership(root)
    return root


_SOURCES_README = """\
These two folders ARE the library. NEXTRON mirrors them.

  VPN Profiles/   .ovpn  .conf  .wgconf  .json
  DNS list/       .txt   .hosts  .list

Add a file here and it appears in NEXTRON. Edit it and it is re-read. Delete it
and it is removed, along with NEXTRON's own copy -- so what you see in the
interface is always exactly what is in these folders, nothing more.

Scanned when NEXTRON starts, and whenever you open the VPN Library (V) or the
DNS Shield (D) screen. A profile that is connected at the time stays until you
disconnect. Blocklists downloaded with U are NEXTRON's own and are not affected.
"""


def _write_sources_readme() -> None:
    readme = sources_dir() / "README.txt"
    if not readme.exists():
        try:
            readme.write_text(_SOURCES_README, encoding="utf-8")
        except OSError:  # pragma: no cover
            pass


def restore_ownership(root: Path | None = None) -> None:
    """Hand anything created under ``sudo`` back to the invoking user.

    Without this, one privileged run leaves root-owned files behind and every
    later unprivileged run fails to save its configuration.
    """
    owner = invoking_user()
    if owner is None:
        return
    uid, gid = owner
    target = root or config_root()
    try:
        for path in (target, *target.rglob("*")):
            try:
                if path.stat().st_uid == 0:
                    os.chown(path, uid, gid)
            except OSError:  # pragma: no cover - races and odd filesystems
                continue
    except OSError:  # pragma: no cover
        pass
