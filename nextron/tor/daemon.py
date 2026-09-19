"""Supervision of the official Tor daemon.

NEXTRON never re-implements Tor. It writes a private ``torrc``, launches the
official ``tor`` binary, follows its bootstrap output line by line, and owns the
process for the lifetime of the session. Attaching to an already running system
daemon is supported as well (``tor.manage_daemon = false``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from nextron.core import process
from nextron.core.config import TorSettings
from nextron.core.events import EventBus, EventType
from nextron.core.exceptions import DependencyError, TorBootstrapError
from nextron.routing.transparent import tor_daemon_uid
from nextron.storage import paths
from nextron.utils import net

log = logging.getLogger(__name__)

__all__ = ["TorDaemon", "TorPorts"]

_BOOTSTRAP_RE = re.compile(
    r"Bootstrapped (?P<percent>\d+)%(?:\s*\((?P<tag>[^)]*)\))?:?\s*(?P<summary>.*)"
)
#: Where a privileged daemon keeps its data. It lives *inside* /var/lib/tor
#: because distributions ship an AppArmor profile for the tor binary that
#: allows that tree and little else -- a daemon pointed anywhere outside it is
#: denied before it can open its DataDirectory.
SYSTEM_RUNTIME_DIR = "/var/lib/tor/nextron"

_SYSTEM_COOKIE_PATHS = (
    Path("/run/tor/control.authcookie"),
    Path("/var/run/tor/control.authcookie"),
    Path("/var/lib/tor/control_auth_cookie"),
)


#: Root causes: tor prints these first, and they name the actual problem.
_ROOT_CAUSE_MARKERS = (
    "could not bind",
    "address already in use",
    "permission denied",
    "no such file",
    "cannot open",
    "unable to open",
    "is not a directory",
    "unrecognized option",
    "unknown option",
)
#: Consequences: tor prints these afterwards, summarising the failure.
_CONSEQUENCE_MARKERS = (
    "failed to parse",
    "failed to validate",
    "failed to bind",
    "reading config failed",
    "[err]",
)


@dataclass(frozen=True, slots=True)
class TorPorts:
    """The ports a daemon instance actually listens on.

    These may differ from the configured preferences: if something already
    holds the preferred port -- most commonly the distribution's own
    ``tor.service`` on 9050 -- NEXTRON moves its private daemon out of the way
    instead of failing.
    """

    socks: int
    control: int
    dns: int
    trans: int
    moved: tuple[str, ...] = ()

    @property
    def relocated(self) -> bool:
        return bool(self.moved)


class TorDaemon:
    """Owns the ``tor`` process and reports bootstrap progress."""

    def __init__(self, settings: TorSettings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._bootstrap_percent = 0
        self._bootstrap_phase = ""
        self._ready = asyncio.Event()
        self._external = False
        self._ports: TorPorts | None = None
        #: Recent daemon output, used to explain a refusal to start.
        self._log_tail: deque[str] = deque(maxlen=60)
        #: Loop time of the last bootstrap line, for stall detection.
        self._last_progress: float = 0.0
        #: Set when the daemon runs under the system Tor account so its own
        #: traffic can be exempted from transparent redirection.
        self._runtime_user: str | None = None
        self._runtime_uid: int | None = None
        self._data_dir: Path = paths.tor_data_dir()
        self._torrc: Path = paths.runtime_dir() / "torrc"

    # -- properties --------------------------------------------------------- #

    @property
    def bootstrap_percent(self) -> int:
        return self._bootstrap_percent

    @property
    def bootstrap_phase(self) -> str:
        return self._bootstrap_phase

    @property
    def running(self) -> bool:
        if self._external:
            return True
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def ports(self) -> TorPorts:
        """The ports in use, falling back to the configured preferences."""
        if self._ports is not None:
            return self._ports
        settings = self._settings
        return TorPorts(
            socks=settings.socks_port,
            control=settings.control_port,
            dns=settings.dns_port,
            trans=settings.trans_port,
        )

    @property
    def socks_port(self) -> int:
        return self.ports.socks

    @property
    def control_port(self) -> int:
        return self.ports.control

    @property
    def dns_port(self) -> int:
        return self.ports.dns

    @property
    def trans_port(self) -> int:
        return self.ports.trans

    @property
    def torrc_path(self) -> Path:
        return self._torrc

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def runtime_uid(self) -> int | None:
        """uid the daemon actually runs as, when that is not this process.

        For a daemon NEXTRON started this is the account it dropped to. For one
        it attached to, the owner of the listening socket -- which is what
        makes transparent routing possible against the system daemon too.
        """
        if self._runtime_uid is not None:
            return self._runtime_uid
        if not self._external:
            return None
        uid = net.listener_uid(self.control_port)
        return uid if uid is not None and uid != os.geteuid() else None

    @property
    def runtime_user(self) -> str | None:
        return self._runtime_user

    @property
    def cookie_path(self) -> Path:
        return self._data_dir / "control_auth_cookie"

    def resolve_cookie(self) -> Path | None:
        """Return the ControlPort cookie for our daemon or the system daemon."""
        if not self._external and self.cookie_path.exists():
            return self.cookie_path
        for candidate in (self.cookie_path, *_SYSTEM_COOKIE_PATHS):
            if candidate.exists():
                return candidate
        return None

    # -- runtime location --------------------------------------------------- #

    def prepare_runtime(self) -> None:
        """Decide where this daemon lives and which account it runs as.

        Transparent (system-wide) routing redirects every outbound connection
        into Tor, which means Tor's own connections to relays must be exempt --
        and the only thing nftables can match on is the uid that opened them.
        A daemon running as the current user therefore cannot be exempted from
        a redirect that applies to the current user.

        So when NEXTRON has root and is asked for transparent routing, it runs
        its daemon under the distribution's Tor account instead, out of a
        directory that account can own.
        """
        settings = self._settings
        user, uid = None, None

        if settings.transparent_routing and process.is_root():
            uid, user = tor_daemon_uid()

        if user is None or uid is None:
            self._runtime_user = self._runtime_uid = None
            self._data_dir = paths.tor_data_dir()
            self._torrc = paths.runtime_dir() / "torrc"
            paths.ensure_layout()
            return

        # Root-owned tree the Tor account can use; also caches the consensus
        # between runs, which makes later bootstraps much faster.
        root = Path(SYSTEM_RUNTIME_DIR)
        data = root / "tor"
        try:
            data.mkdir(parents=True, exist_ok=True)
            root.chmod(0o755)
            entry = pwd.getpwnam(user)
            os.chown(data, entry.pw_uid, entry.pw_gid)
            data.chmod(0o700)
        except (OSError, KeyError) as exc:
            log.warning(
                "Cannot prepare %s for the %s account (%s); falling back to a "
                "private daemon without transparent routing",
                data,
                user,
                exc,
            )
            self._runtime_user = self._runtime_uid = None
            self._data_dir = paths.tor_data_dir()
            self._torrc = paths.runtime_dir() / "torrc"
            paths.ensure_layout()
            return

        self._runtime_user, self._runtime_uid = user, uid
        self._data_dir = data
        self._torrc = root / "torrc"
        log.info("Tor daemon will run as %s (uid %s) from %s", user, uid, data)

    def launch_command(self, binary: str, torrc: Path) -> tuple[str, ...]:
        """The argv used to start the daemon, dropping privileges if needed."""
        if self._runtime_user is None:
            return (binary, "-f", str(torrc))
        # runuser is part of util-linux and needs no password when root.
        runuser = process.which("runuser")
        if runuser:
            return (runuser, "-u", self._runtime_user, "--", binary, "-f", str(torrc))
        return ("setpriv", "--reuid", self._runtime_user, "--", binary, "-f", str(torrc))

    # -- port resolution ---------------------------------------------------- #

    def resolve_ports(self) -> TorPorts:
        """Pick the ports for this daemon, stepping around anything in use.

        The distribution's ``tor.service`` listens on 9050 by default, so a
        private daemon started with the same preference dies instantly with
        "Address already in use". Rather than fail -- or silently hand the user
        a daemon they cannot control -- NEXTRON relocates its own listeners and
        says so.
        """
        settings = self._settings
        wanted = [
            ("SOCKS", settings.socks_port),
            ("ControlPort", settings.control_port),
            ("DNSPort", settings.dns_port),
        ]
        if settings.transparent_routing:
            wanted.append(("TransPort", settings.trans_port))

        resolved: dict[str, int] = {}
        moved: list[str] = []
        taken: set[int] = set()

        for label, preferred in wanted:
            try:
                port = net.find_free_port(preferred, exclude=taken)
            except OSError as exc:
                raise TorBootstrapError(
                    f"Cannot find a free port for Tor's {label}: {exc}"
                ) from exc
            taken.add(port)
            resolved[label] = port
            if port != preferred:
                moved.append(f"{label} {preferred}->{port}")
                log.info("Tor %s moved from %s to %s (in use)", label, preferred, port)

        self._ports = TorPorts(
            socks=resolved["SOCKS"],
            control=resolved["ControlPort"],
            dns=resolved["DNSPort"],
            trans=resolved.get("TransPort", settings.trans_port),
            moved=tuple(moved),
        )
        return self._ports

    # -- torrc -------------------------------------------------------------- #

    def build_torrc(self) -> str:
        """Render the private ``torrc`` for this session."""
        settings = self._settings
        ports = self.ports
        lines = [
            "# Generated by NEXTRON -- do not edit, it is rewritten on every launch",
            "ClientOnly 1",
            "AvoidDiskWrites 1",
            f"DataDirectory {self._data_dir}",
            f"SocksPort 127.0.0.1:{ports.socks}",
            f"ControlPort 127.0.0.1:{ports.control}",
            "CookieAuthentication 1",
            f"CookieAuthFile {self.cookie_path}",
            f"DNSPort 127.0.0.1:{ports.dns}",
            "AutomapHostsOnResolve 1",
            "VirtualAddrNetworkIPv4 10.192.0.0/10",
            "Log notice stdout",
            "SafeLogging 1",
        ]
        if settings.transparent_routing:
            lines.append(f"TransPort 127.0.0.1:{ports.trans}")
        if settings.exit_countries:
            countries = ",".join(f"{{{c.lower()}}}" for c in settings.exit_countries)
            lines.append(f"ExitNodes {countries}")
            lines.append(f"StrictNodes {1 if settings.strict_exit_nodes else 0}")
        return "\n".join(lines) + "\n"

    def write_torrc(self) -> Path:
        paths.ensure_layout()
        path = self.torrc_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.build_torrc(), encoding="utf-8")
        # The Tor account must be able to read a config it did not write.
        path.chmod(0o644)
        log.debug("Wrote torrc to %s", path)
        return path

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Launch (or attach to) Tor and wait for a 100% bootstrap."""
        settings = self._settings

        if not settings.manage_daemon:
            await self._attach_external()
            return

        # A daemon we can actually drive is one with a reachable ControlPort.
        if await net.is_port_open("127.0.0.1", settings.control_port):
            log.info(
                "ControlPort %s already serving; attaching to the running daemon",
                settings.control_port,
            )
            await self._attach_external()
            return

        binary = process.which(settings.binary)
        if not binary:
            raise DependencyError(
                "The 'tor' daemon is not installed. Install it with: "
                "sudo apt install tor"
            )

        self._reset()
        self.prepare_runtime()
        ports = self.resolve_ports()
        if ports.relocated:
            # The usual cause is the distribution's tor.service on 9050.
            self._bus.emit(
                EventType.ACTIVITY,
                "Another Tor is already listening; NEXTRON moved its own "
                f"daemon: {', '.join(ports.moved)}",
                level="warning",
            )

        torrc = self.write_torrc()
        self._bus.emit(
            EventType.TOR_STARTING,
            f"Launching Tor daemon (SOCKS {ports.socks}, control {ports.control})",
            socks_port=ports.socks,
            control_port=ports.control,
        )

        if self._runtime_user:
            self._bus.emit(
                EventType.ACTIVITY,
                f"Running Tor as '{self._runtime_user}' so its own traffic can "
                "be exempted from system-wide redirection",
            )
        self._process = await process.spawn(*self.launch_command(binary, torrc))
        self._reader_task = asyncio.create_task(
            self._follow_output(), name="tor-output-reader"
        )
        await self._await_bootstrap()

        if not await net.wait_for_port("127.0.0.1", ports.socks, timeout=15):
            await self.stop()
            raise TorBootstrapError(
                f"Tor bootstrapped but the SOCKS port {ports.socks} never opened"
            )
        log.info(
            "Tor daemon ready (pid %s, SOCKS %s, control %s)",
            self.pid,
            ports.socks,
            ports.control,
        )

    async def _await_bootstrap(self) -> None:
        """Wait for a 100% bootstrap, aborting early on death or a real stall.

        A total time limit is the wrong test: a healthy bootstrap takes
        anywhere from a few seconds to a couple of minutes depending on the
        connection, while a *stuck* one stops emitting progress entirely. So
        ``bootstrap_timeout`` is applied to the gap between progress lines, and
        a separate ceiling stops NEXTRON waiting forever.
        """
        assert self._process is not None
        settings = self._settings
        loop = asyncio.get_running_loop()

        stall_limit = float(settings.bootstrap_timeout)
        hard_limit = max(stall_limit * 4, 600.0)
        started = loop.time()
        self._last_progress = started

        ready = asyncio.create_task(self._ready.wait(), name="tor-ready")
        exited = asyncio.create_task(self._process.wait(), name="tor-exited")
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {ready, exited}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                if self._ready.is_set():
                    return

                now = loop.time()

                if exited in done:
                    code = self._process.returncode
                    reason = await self._failure_reason()
                    await self.stop()
                    raise TorBootstrapError(self._explain_exit(code, reason))

                if now - self._last_progress > stall_limit:
                    raise TorBootstrapError(await self._explain_stall(stall_limit))

                if now - started > hard_limit:
                    raise TorBootstrapError(
                        await self._explain_stall(hard_limit, ceiling=True)
                    )
        finally:
            for task in (ready, exited):
                if not task.done():
                    task.cancel()

    async def _explain_stall(self, limit: float, *, ceiling: bool = False) -> str:
        """Describe a stalled bootstrap. Reads the counters *before* teardown."""
        # stop() resets these, so capture them first.
        percent = self._bootstrap_percent
        phase = self._bootstrap_phase or "no progress reported"
        await self.stop()

        if ceiling:
            opening = f"Tor was still bootstrapping after {limit:.0f}s"
        else:
            opening = f"Tor stopped making progress for {limit:.0f}s"

        message = f"{opening} (reached {percent}% -- {phase})"
        if percent < 10:
            message += (
                ". Tor could not reach the network at all: check the internet "
                "connection, or a firewall or captive portal blocking it."
            )
        else:
            message += (
                ". This is usually a slow or filtered connection. Try again, or "
                "raise 'Bootstrap timeout' in Settings (S)."
            )
        return message

    async def _failure_reason(self) -> str:
        """Pick the most informative line out of the daemon's output."""
        # Give the reader a moment to drain whatever tor wrote before exiting.
        if self._reader_task is not None and not self._reader_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._reader_task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass

        # Scan oldest-first: tor states the root cause ("Could not bind to
        # 127.0.0.1:9050: Address already in use") before the summaries it
        # derives from it ("Reading config failed--see warnings above").
        lines = list(self._log_tail)
        for markers in (_ROOT_CAUSE_MARKERS, _CONSEQUENCE_MARKERS):
            for line in lines:
                if any(marker in line.lower() for marker in markers):
                    return _strip_log_prefix(line)
        return _strip_log_prefix(lines[-1]) if lines else ""

    def _explain_exit(self, code: int | None, reason: str) -> str:
        """Turn a daemon exit into a message that names the fix."""
        ports = self.ports
        message = f"The Tor daemon exited immediately (code {code})"
        if reason:
            message += f": {reason}"

        lowered = reason.lower()
        if "already in use" in lowered or "could not bind" in lowered:
            message += (
                f". Something else is listening on one of Tor's ports "
                f"(SOCKS {ports.socks}, control {ports.control}, DNS {ports.dns}). "
                "Stop the other daemon with 'sudo systemctl stop tor', or change "
                "the ports in Settings (S)."
            )
        elif "permission denied" in lowered:
            message += (
                f". Check that {self._data_dir} is writable and owned by "
                "you."
            )
        elif "failed to parse" in lowered or "failed to validate" in lowered:
            message += f". The generated configuration is at {self.torrc_path}."
        return message

    async def _attach_external(self) -> None:
        """Use a Tor daemon that somebody else started."""
        settings = self._settings
        if not await net.is_port_open("127.0.0.1", settings.control_port):
            hint = ""
            if net.listening_on("127.0.0.1", settings.socks_port):
                # The classic Debian/Ubuntu situation: tor.service is running
                # with its SOCKS port open and its ControlPort switched off.
                hint = (
                    f" A Tor daemon is listening on {settings.socks_port} but "
                    "offers no ControlPort, so its identity cannot be rotated. "
                    "Either add 'ControlPort 9051' and 'CookieAuthentication 1' "
                    "to /etc/tor/torrc and restart it, or turn "
                    "'Manage own daemon' back on in Settings (S) to let NEXTRON "
                    "run its own."
                )
            raise TorBootstrapError(
                f"No Tor ControlPort on 127.0.0.1:{settings.control_port}."
                + (hint or " Start the system daemon with: sudo systemctl start tor")
            )

        # No cookie pre-check here: the daemon reports its own cookie path over
        # PROTOCOLINFO, which may differ from anything guessable, and the
        # controller explains a permission failure when one actually happens.
        self._external = True
        self._ports = TorPorts(
            socks=settings.socks_port,
            control=settings.control_port,
            dns=settings.dns_port,
            trans=settings.trans_port,
        )
        self._bootstrap_percent = 100
        self._bootstrap_phase = "attached to existing daemon"
        self._ready.set()
        self._bus.emit(
            EventType.TOR_BOOTSTRAP,
            "Attached to running Tor daemon",
            percent=100,
            phase=self._bootstrap_phase,
        )

    async def stop(self) -> None:
        """Terminate the daemon we own; leave a foreign daemon alone."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        if self._external:
            self._external = False
            self._reset()
            return

        if self._process is not None:
            await process.terminate(self._process, timeout=10)
            log.info("Tor daemon stopped")
            self._process = None

        self._reset()
        self._bus.emit(EventType.TOR_STOPPED, "Tor daemon stopped")

    def _reset(self) -> None:
        self._bootstrap_percent = 0
        self._bootstrap_phase = ""
        self._ready = asyncio.Event()
        self._log_tail.clear()
        self._last_progress = 0.0

    # -- output following --------------------------------------------------- #

    async def _follow_output(self) -> None:
        """Parse the daemon's notice log for bootstrap progress and warnings."""
        assert self._process is not None
        stream = self._process.stdout
        if stream is None:  # pragma: no cover
            return

        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            self._log_tail.append(line)
            log.debug("tor: %s", line)

            match = _BOOTSTRAP_RE.search(line)
            if match:
                percent = int(match.group("percent"))
                phase = (match.group("summary") or match.group("tag") or "").strip()
                self._bootstrap_percent = percent
                self._bootstrap_phase = phase
                self._last_progress = asyncio.get_running_loop().time()
                self._bus.emit(
                    EventType.TOR_BOOTSTRAP,
                    f"Bootstrap {percent}% -- {phase}"
                    if phase
                    else f"Bootstrap {percent}%",
                    percent=percent,
                    phase=phase,
                )
                if percent >= 100:
                    self._ready.set()
                continue

            if "[warn]" in line or "[err]" in line:
                self._bus.emit(
                    EventType.ERROR,
                    f"Tor: {line.split('] ', 1)[-1]}",
                    level="warning",
                    source="tor",
                )

        code = self._process.returncode
        if code not in (None, 0) and not self._ready.is_set():
            self._bus.emit(
                EventType.ERROR,
                f"Tor daemon exited unexpectedly (code {code})",
                level="error",
                source="tor",
            )


def _strip_log_prefix(line: str) -> str:
    """``"Sep 18 21:14:40.000 [warn] Could not bind"`` -> ``"Could not bind"``."""
    for marker in ("[warn] ", "[err] ", "[notice] "):
        position = line.find(marker)
        if position != -1:
            return line[position + len(marker) :].strip()
    return line.strip()
