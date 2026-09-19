"""OpenVPN backend.

The official ``openvpn`` binary is driven directly. Its stdout is followed line
by line so the tunnel device, the assigned address and the definitive
"Initialization Sequence Completed" marker are observed rather than guessed.

When a profile must be tunnelled through Tor (VPN over Tor) a *runtime* copy of
the configuration is generated with ``socks-proxy`` injected -- the user's
original profile is never modified.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from nextron.core import process
from nextron.core.exceptions import VPNConnectionError, VPNCredentialsRequired
from nextron.storage import paths
from nextron.vpn.profiles import VPNProfile, wants_credentials

log = logging.getLogger(__name__)

__all__ = ["OpenVPNBackend", "OpenVPNResult"]

_READY_MARKER = "Initialization Sequence Completed"
_DEVICE_RE = re.compile(r"TUN/TAP device (?P<dev>[\w.-]+) opened")
_IFCONFIG_RE = re.compile(r"ifconfig (?P<ip>\d+\.\d+\.\d+\.\d+)")
_PUSH_IP_RE = re.compile(r"ifconfig_local=(?P<ip>\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_FATAL_MARKERS = (
    "AUTH_FAILED",
    "Failed retrieving username or password",
    "Error opening --auth-user-pass file",
    "TLS Error: TLS handshake failed",
    "Cannot resolve host address",
    "Exiting due to fatal error",
    "Options error",
    "All TAP-Windows adapters",
)


#: Startup chatter that explains nothing when a connection times out.
_NOISE = (
    "DEPRECATED OPTION",
    "library versions:",
    "DCO version:",
    "OpenVPN 2.",
    "Note: '--allow-compression'",
    "WARNING: file",
)


def _meaningful_tail(lines: tuple[str, ...], count: int = 3) -> str:
    """The last lines that say something about why a tunnel never came up."""
    useful = [
        line for line in lines if not any(noise in line for noise in _NOISE)
    ]
    return " | ".join((useful or list(lines))[-count:])


@dataclass(slots=True)
class OpenVPNResult:
    """What the backend learned while bringing a tunnel up."""

    interface: str | None = None
    tunnel_ip: str | None = None
    ready: bool = False
    error: str | None = None


class OpenVPNBackend:
    """Manage a single ``openvpn`` child process."""

    def __init__(self, binary: str = "openvpn") -> None:
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._result = OpenVPNResult()
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._log_tail: list[str] = []

    # -- properties --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._result.ready
        )

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def result(self) -> OpenVPNResult:
        return self._result

    @property
    def log_tail(self) -> tuple[str, ...]:
        return tuple(self._log_tail[-40:])

    # -- configuration ------------------------------------------------------ #

    def runtime_config(
        self,
        profile: VPNProfile,
        *,
        socks_proxy: tuple[str, int] | None = None,
    ) -> Path:
        """Materialise the configuration actually handed to ``openvpn``."""
        text = profile.read_config()
        directives: list[str] = []

        if profile.auth_file:
            # Point OpenVPN at a credentials file the user supplied themselves;
            # NEXTRON never prompts for or stores VPN passwords.
            text = re.sub(
                r"^\s*auth-user-pass.*$",
                "",
                text,
                flags=re.MULTILINE,
            )
            directives.append(f"auth-user-pass {profile.auth_file}")
        elif wants_credentials(text):
            # Without this, OpenVPN queries for the password -- and with no
            # console it hands the question to systemd's password agent and
            # waits there until the connection times out.
            raise VPNCredentialsRequired(
                f"'{profile.name}' needs a username and password. Open the VPN "
                "Library (V), highlight it and press U to point it at a "
                "credentials file: two lines, username then password."
            )

        if socks_proxy is not None:
            host, port = socks_proxy
            if not profile.tcp:
                raise VPNConnectionError(
                    f"'{profile.name}' is a UDP profile. Routing a VPN through "
                    "Tor requires a TCP OpenVPN profile (proto tcp)."
                )
            text = re.sub(r"^\s*socks-proxy.*$", "", text, flags=re.MULTILINE)
            directives.extend([f"socks-proxy {host} {port}", "socks-proxy-retry"])
            if not host.startswith("127."):
                # A remote SOCKS hop must stay reachable outside the tunnel it
                # is carrying; a loopback hop needs no route exclusion.
                directives.append(f"route {host} 255.255.255.255 net_gateway")

        directives.extend(
            [
                # Reduce restart churn so a transition is a clean up/down cycle.
                "resolv-retry 10",
                "connect-retry-max 3",
                # Never sit waiting for an answer nobody can type.
                "auth-retry nointeract",
                "verb 3",
            ]
        )

        header = "# NEXTRON runtime configuration -- regenerated on every connect\n"
        body = text.rstrip() + "\n\n# --- NEXTRON injected directives ---\n"
        body += "\n".join(directives) + "\n"

        paths.ensure_layout()
        target = paths.runtime_dir() / f"openvpn-{profile.id}.conf"
        target.write_text(header + body, encoding="utf-8")
        target.chmod(0o600)
        return target

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(
        self,
        profile: VPNProfile,
        *,
        timeout: float = 60.0,
        socks_proxy: tuple[str, int] | None = None,
    ) -> OpenVPNResult:
        """Bring the tunnel up and wait for OpenVPN's own ready marker."""
        if self.running:
            await self.disconnect()

        binary = process.which(self._binary)
        if not binary:
            raise VPNConnectionError(
                "OpenVPN is not installed. Install it with: sudo apt install openvpn"
            )

        config = self.runtime_config(profile, socks_proxy=socks_proxy)
        self._result = OpenVPNResult()
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._log_tail.clear()

        log.info("Starting OpenVPN for '%s'", profile.name)
        # Deliberately no --log-append: it redirects OpenVPN's output into the
        # file and leaves stdout empty, so the reader below would never see
        # "Initialization Sequence Completed" and every tunnel would time out.
        # The reader writes the log itself instead.
        self._process = await process.spawn_privileged(
            binary,
            "--config",
            str(config),
            "--cd",
            str(config.parent),
        )
        self._reader = asyncio.create_task(
            self._follow_output(), name=f"openvpn-{profile.id}"
        )

        ready_task = asyncio.create_task(self._ready.wait())
        failed_task = asyncio.create_task(self._failed.wait())
        done, pending = await asyncio.wait(
            {ready_task, failed_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if failed_task in done:
            error = self._result.error or "OpenVPN reported a fatal error"
            await self.disconnect()
            raise VPNConnectionError(f"{profile.name}: {error}")
        if ready_task not in done:
            tail = _meaningful_tail(self.log_tail)
            await self.disconnect()
            raise VPNConnectionError(
                f"{profile.name}: tunnel not established within {timeout:.0f}s"
                + (f" ({tail})" if tail else "")
            )

        self._result.ready = True
        log.info(
            "OpenVPN connected: %s (%s)",
            self._result.interface or "tun?",
            self._result.tunnel_ip or "no address",
        )
        return self._result

    async def disconnect(self) -> None:
        """Terminate the tunnel process."""
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None

        if self._process is not None:
            await process.terminate(self._process, timeout=10, privileged=True)
            self._process = None
            log.info("OpenVPN disconnected")

        self._result = OpenVPNResult()

    # -- output ------------------------------------------------------------- #

    async def _follow_output(self) -> None:
        assert self._process is not None
        stream = self._process.stdout
        if stream is None:  # pragma: no cover
            return

        try:
            paths.ensure_layout()
            logfile = (paths.logs_dir() / "openvpn.log").open("a", encoding="utf-8")
        except OSError:  # pragma: no cover - a log we cannot write is not fatal
            logfile = None

        try:
            await self._read_lines(stream, logfile)
        finally:
            if logfile is not None:
                logfile.close()

        if self._process.returncode not in (None, 0) and not self._ready.is_set():
            self._result.error = self._result.error or (
                f"openvpn exited with code {self._process.returncode}"
            )
            self._failed.set()

    async def _read_lines(self, stream, logfile) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            self._log_tail.append(line)
            log.debug("openvpn: %s", line)
            if logfile is not None:
                logfile.write(line + "\n")
                logfile.flush()

            device = _DEVICE_RE.search(line)
            if device:
                self._result.interface = device.group("dev")

            address = _IFCONFIG_RE.search(line) or _PUSH_IP_RE.search(line)
            if address:
                self._result.tunnel_ip = address.group("ip")

            if _READY_MARKER in line:
                self._ready.set()
                continue

            for marker in _FATAL_MARKERS:
                if marker in line:
                    self._result.error = line.split("] ", 1)[-1][:200]
                    self._failed.set()
                    break
