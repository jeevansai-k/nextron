"""Stem ControlPort integration.

Stem's API is synchronous, so every call is pushed onto a worker thread with
``asyncio.to_thread`` and asynchronous Tor events are marshalled back onto the
event loop with ``call_soon_threadsafe``. Nothing here ever blocks the TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from stem import CircStatus, Signal, SocketError
from stem.connection import AuthenticationFailure, authenticate_cookie
from stem.control import Controller
from stem.control import EventType as StemEventType

from nextron.core.config import TorSettings
from nextron.core.events import EventBus, EventType
from nextron.core.exceptions import TorControlError
from nextron.utils import net

log = logging.getLogger(__name__)

__all__ = ["CircuitInfo", "TorController"]

#: Tor coalesces NEWNYM signals issued less than this far apart.
NEWNYM_RATE_LIMIT = 10.0

#: Circuit purposes that carry ordinary user traffic. Tor 0.4.8+ may split a
#: stream across "conflux" circuits, so those count too; one-hop directory
#: circuits share the GENERAL purpose and are filtered out by hop count.
_TRAFFIC_PURPOSES = frozenset({"GENERAL", "CONFLUX_LINKED", ""})

#: A real client circuit is three hops; anything shorter is a directory fetch.
_MIN_REAL_HOPS = 3


@dataclass(frozen=True, slots=True)
class CircuitInfo:
    """A snapshot of one Tor circuit."""

    id: str
    status: str
    purpose: str
    path: tuple[str, ...]
    created: datetime | None = None

    @property
    def exit_nickname(self) -> str | None:
        return self.path[-1] if self.path else None

    @property
    def hops(self) -> int:
        return len(self.path)

    @property
    def pretty_path(self) -> str:
        return " -> ".join(self.path) if self.path else "--"


class TorController:
    """Async facade over :class:`stem.control.Controller`."""

    def __init__(self, settings: TorSettings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._controller: Controller | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._port: int = settings.control_port
        self._last_newnym: float = 0.0
        self._lock = asyncio.Lock()

    # -- properties --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._controller is not None and self._controller.is_alive()

    @property
    def port(self) -> int:
        """The ControlPort this controller is talking to."""
        return self._port

    # -- connection --------------------------------------------------------- #

    async def connect(
        self, cookie_path: Path | None = None, *, port: int | None = None
    ) -> None:
        """Authenticate against the ControlPort.

        *port* overrides the configured preference, because the daemon may have
        had to move its listener out of the way of another Tor instance.
        """
        if self.connected:
            return
        self._loop = asyncio.get_running_loop()
        port = port or self._settings.control_port
        self._port = port

        if not await net.wait_for_port("127.0.0.1", port, timeout=20):
            raise TorControlError(f"ControlPort 127.0.0.1:{port} is not reachable")

        def _open() -> Controller:
            controller = Controller.from_port(address="127.0.0.1", port=port)
            try:
                # Stem learns the cookie's location from the daemon's own
                # PROTOCOLINFO reply, which is the authoritative answer and
                # handles safe-cookie authentication for us.
                controller.authenticate()
            except AuthenticationFailure:
                if cookie_path is None:
                    controller.close()
                    raise
                # Fall back to the file we know about: useful when the daemon
                # reports a path this process cannot use (a chroot, say).
                try:
                    authenticate_cookie(controller, str(cookie_path))
                except AuthenticationFailure:
                    controller.close()
                    raise
            return controller

        try:
            self._controller = await asyncio.to_thread(_open)
        except AuthenticationFailure as exc:
            raise TorControlError(self._explain_auth_failure(exc, cookie_path)) from exc
        except (SocketError, OSError) as exc:
            raise TorControlError(
                f"Cannot reach the Tor ControlPort on 127.0.0.1:{port}: {exc}"
            ) from exc

        version = await self.version()
        log.info("ControlPort connected (Tor %s)", version or "unknown")
        await self._register_events()

    @staticmethod
    def _explain_auth_failure(
        error: Exception, cookie_path: Path | None
    ) -> str:
        """Turn a Stem authentication failure into something actionable."""
        message = f"Tor rejected the control connection: {error}"
        if cookie_path is None:
            return (
                message
                + ". The daemon may have no authentication configured; add "
                "'CookieAuthentication 1' to its torrc."
            )
        if not cookie_path.exists():
            return message + f". The cookie file {cookie_path} does not exist."
        if not os.access(cookie_path, os.R_OK):
            return (
                message
                + f". The cookie file {cookie_path} is not readable by this "
                "user -- add yourself to the daemon's group (sudo usermod -aG "
                "debian-tor $USER, then log out and back in), or let NEXTRON "
                "manage its own daemon."
            )
        return message

    async def close(self) -> None:
        controller, self._controller = self._controller, None
        if controller is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(controller.close)
        log.debug("ControlPort connection closed")

    def _require(self) -> Controller:
        if self._controller is None or not self._controller.is_alive():
            raise TorControlError("ControlPort is not connected")
        return self._controller

    # -- event subscription ------------------------------------------------- #

    async def _register_events(self) -> None:
        """Forward Tor's CIRC and STATUS_CLIENT events onto the NEXTRON bus."""
        controller = self._require()

        def on_circuit(event) -> None:  # runs on a stem thread
            self._publish_threadsafe(
                EventType.TOR_CIRCUIT,
                f"Circuit {event.id} {str(event.status).lower()}",
                circuit_id=str(event.id),
                status=str(event.status),
                path=tuple(nick for _, nick in getattr(event, "path", ()) or ()),
            )

        def on_status(event) -> None:  # runs on a stem thread
            action = str(getattr(event, "action", ""))
            if action.upper() == "BOOTSTRAP":
                arguments = getattr(event, "arguments", {}) or {}
                percent = int(arguments.get("PROGRESS", 0) or 0)
                self._publish_threadsafe(
                    EventType.TOR_BOOTSTRAP,
                    f"Bootstrap {percent}%",
                    percent=percent,
                    phase=arguments.get("SUMMARY", ""),
                )

        def _subscribe() -> None:
            controller.add_event_listener(on_circuit, StemEventType.CIRC)
            controller.add_event_listener(on_status, StemEventType.STATUS_CLIENT)

        with contextlib.suppress(Exception):
            await asyncio.to_thread(_subscribe)

    def _publish_threadsafe(self, event_type: EventType, message: str, **data) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            lambda: self._bus.emit(event_type, message, **data)
        )

    # -- queries ------------------------------------------------------------ #

    async def version(self) -> str | None:
        try:
            controller = self._require()
            value = await asyncio.to_thread(controller.get_version)
        except Exception:
            return None
        return str(value)

    async def bootstrap_phase(self) -> tuple[int, str]:
        """Return ``(percent, summary)`` from ``status/bootstrap-phase``."""
        try:
            controller = self._require()
            raw = await asyncio.to_thread(
                controller.get_info, "status/bootstrap-phase", ""
            )
        except Exception:
            return 0, ""
        percent = 0
        summary = ""
        for token in str(raw).split():
            if token.startswith("PROGRESS="):
                with contextlib.suppress(ValueError):
                    percent = int(token.split("=", 1)[1])
            elif token.startswith("SUMMARY="):
                summary = token.split("=", 1)[1].strip('"')
        return percent, summary

    async def circuits(self) -> list[CircuitInfo]:
        """Return every known circuit."""
        try:
            controller = self._require()
            raw = await asyncio.to_thread(controller.get_circuits)
        except Exception as exc:
            log.debug("Circuit enumeration failed: %s", exc)
            return []

        result: list[CircuitInfo] = []
        for circuit in raw:
            result.append(
                CircuitInfo(
                    id=str(circuit.id),
                    status=str(circuit.status),
                    purpose=str(getattr(circuit, "purpose", "") or ""),
                    path=tuple(nickname for _, nickname in circuit.path),
                    created=getattr(circuit, "created", None),
                )
            )
        return result

    async def streams(self) -> list[tuple[str, str]]:
        """Return ``(stream_id, circuit_id)`` for every attached stream."""
        try:
            controller = self._require()
            raw = await asyncio.to_thread(controller.get_streams)
        except Exception as exc:
            log.debug("Stream enumeration failed: %s", exc)
            return []
        return [
            (str(stream.id), str(stream.circ_id))
            for stream in raw
            if getattr(stream, "circ_id", None)
        ]

    async def active_circuit(self) -> CircuitInfo | None:
        """Return the circuit actually carrying traffic.

        Tor keeps one-hop directory circuits and pre-built spares alongside the
        circuit in use, so "the newest built circuit" is not good enough. The
        stream table says which circuit real traffic is attached to; only when
        nothing is attached does this fall back to the newest fully built
        multi-hop circuit.
        """
        circuits = await self.circuits()
        if not circuits:
            return None

        built = [
            circuit
            for circuit in circuits
            if circuit.status == str(CircStatus.BUILT)
            and circuit.purpose.upper() in _TRAFFIC_PURPOSES
        ]
        if not built:
            return None

        by_id = {circuit.id: circuit for circuit in built}
        for _stream_id, circuit_id in await self.streams():
            carrying = by_id.get(circuit_id)
            if carrying is not None:
                return carrying

        # Nothing attached right now. Report a *complete* circuit or nothing:
        # tor keeps one-hop circuits for directory fetches, and presenting one
        # as "your path through the network" would be a lie.
        complete = [circuit for circuit in built if circuit.hops >= _MIN_REAL_HOPS]
        if not complete:
            return None
        return max(complete, key=lambda c: int(c.id) if c.id.isdigit() else 0)

    async def exit_fingerprint(self) -> str | None:
        """Fingerprint of the exit relay on the active circuit."""
        circuit = await self.active_circuit()
        if circuit is None:
            return None
        try:
            controller = self._require()
            raw = await asyncio.to_thread(controller.get_circuits)
        except Exception:
            return None
        for candidate in raw:
            if str(candidate.id) == circuit.id and candidate.path:
                return str(candidate.path[-1][0])
        return None

    async def country_of(self, ip: str) -> str | None:
        """Ask Tor's bundled GeoIP database for the country of *ip*."""
        try:
            controller = self._require()
            raw = await asyncio.to_thread(
                controller.get_info, f"ip-to-country/{ip}", ""
            )
        except Exception:
            return None
        value = str(raw).strip().upper()
        return value if value and value != "??" else None

    async def traffic(self) -> tuple[int, int]:
        """Return ``(bytes_read, bytes_written)`` since the daemon started."""
        try:
            controller = self._require()
            read = await asyncio.to_thread(controller.get_info, "traffic/read", "0")
            written = await asyncio.to_thread(controller.get_info, "traffic/written", "0")
            return int(read), int(written)
        except Exception:
            return 0, 0

    # -- actions ------------------------------------------------------------ #

    async def newnym(self, *, wait_for_rate_limit: bool = True) -> bool:
        """Request a brand new identity (NEWNYM).

        Tor silently coalesces signals sent less than ten seconds apart, so by
        default the call waits out the remaining window to guarantee the new
        identity the user asked for.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_newnym
            if wait_for_rate_limit and self._last_newnym and elapsed < NEWNYM_RATE_LIMIT:
                delay = NEWNYM_RATE_LIMIT - elapsed
                log.debug("Waiting %.1fs for the NEWNYM rate limit", delay)
                await asyncio.sleep(delay)

            try:
                controller = self._require()
                await asyncio.to_thread(controller.signal, Signal.NEWNYM)
            except Exception as exc:
                log.error("NEWNYM failed: %s", exc)
                return False

            self._last_newnym = loop.time()
            log.info("NEWNYM signalled -- new Tor identity requested")
            return True

    async def close_circuit(self, circuit_id: str) -> bool:
        try:
            controller = self._require()
            await asyncio.to_thread(controller.close_circuit, circuit_id)
            return True
        except Exception as exc:
            log.debug("Closing circuit %s failed: %s", circuit_id, exc)
            return False

    async def new_circuit(self, *, await_build: bool = True) -> str | None:
        """Explicitly build a fresh circuit and return its id."""
        try:
            controller = self._require()
            circuit_id = await asyncio.to_thread(
                controller.new_circuit, await_build=await_build
            )
            return str(circuit_id)
        except Exception as exc:
            log.debug("Explicit circuit build failed: %s", exc)
            return None
