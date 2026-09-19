"""Async process helpers and privilege handling.

NEXTRON drives real system binaries (``tor``, ``openvpn``, ``wg-quick``,
``nft``, ``ip``). Every one of them is invoked through this module so that
timeouts, logging, privilege escalation and graceful shutdown behave
identically everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from dataclasses import dataclass

from nextron.core.exceptions import DependencyError, PrivilegeError

log = logging.getLogger(__name__)

__all__ = [
    "CommandResult",
    "is_root",
    "privileged_prefix",
    "require_binary",
    "run",
    "run_privileged",
    "spawn",
    "spawn_privileged",
    "sudo_available",
    "terminate",
    "which",
]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of a completed command."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout when present, otherwise stderr -- convenient for probes."""
        return self.stdout.strip() or self.stderr.strip()

    def first_line(self) -> str:
        return self.output.splitlines()[0] if self.output else ""


def is_root() -> bool:
    """True when the current process has uid 0."""
    return os.geteuid() == 0


def which(binary: str) -> str | None:
    """Locate *binary*, also searching the sbin directories."""
    found = shutil.which(binary)
    if found:
        return found
    extra = os.pathsep.join(("/usr/sbin", "/sbin", "/usr/local/sbin"))
    return shutil.which(binary, path=os.environ.get("PATH", "") + os.pathsep + extra)


def require_binary(binary: str) -> str:
    """Return the absolute path of *binary* or raise :class:`DependencyError`."""
    found = which(binary)
    if not found:
        raise DependencyError(f"Required binary '{binary}' is not installed")
    return found


def sudo_available() -> bool:
    """True when ``sudo`` can be used without an interactive prompt."""
    return which("sudo") is not None


def privileged_prefix() -> tuple[str, ...]:
    """Return the prefix needed to run a command as root.

    Empty when already root. Uses non-interactive sudo otherwise so a missing
    cached credential fails fast instead of hanging a TUI on a password prompt.
    """
    if is_root():
        return ()
    if sudo_available():
        return ("sudo", "-n")
    raise PrivilegeError(
        "Root privileges are required and 'sudo' is unavailable. "
        "Re-run NEXTRON with sudo."
    )


async def run(
    *command: str,
    timeout: float = 30.0,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    quiet: bool = False,
) -> CommandResult:
    """Run *command* to completion and capture its output."""
    if not command:
        raise ValueError("run() requires at least one argument")

    if not quiet:
        log.debug("exec: %s", " ".join(command))

    merged_env = {**os.environ, **(env or {})}
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
    except FileNotFoundError as exc:
        raise DependencyError(f"Binary not found: {command[0]}") from exc

    payload = stdin.encode() if stdin is not None else None
    try:
        raw_out, raw_err = await asyncio.wait_for(
            process.communicate(payload), timeout=timeout
        )
    except TimeoutError:
        await terminate(process)
        log.warning("Command timed out after %.0fs: %s", timeout, " ".join(command))
        return CommandResult(tuple(command), 124, "", "timed out", timed_out=True)

    result = CommandResult(
        command=tuple(command),
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=raw_out.decode(errors="replace"),
        stderr=raw_err.decode(errors="replace"),
    )

    if not result.ok and not quiet:
        log.debug(
            "Command failed (rc=%s): %s :: %s",
            result.returncode,
            " ".join(command),
            result.stderr.strip()[:300],
        )
    if check and not result.ok:
        raise DependencyError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()[:500]}"
        )
    return result


async def run_privileged(*command: str, **kwargs) -> CommandResult:
    """Run *command* as root, prefixing ``sudo -n`` when necessary."""
    return await run(*privileged_prefix(), *command, **kwargs)


async def spawn(
    *command: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> asyncio.subprocess.Process:
    """Start a long-lived process with piped output (for log streaming)."""
    log.debug("spawn: %s", " ".join(command))
    merged_env = {**os.environ, **(env or {})}
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            # Never inherit the terminal the TUI is drawing on: a child that
            # prompts (OpenVPN asking for a password) would block for as long
            # as we wait for it, and scribble over the interface.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged_env,
            cwd=cwd,
            # Its own process group, so helpers that fork (runuser, sudo) can
            # be torn down whole. Signalling only the wrapper leaves the real
            # daemon running, which then looks like a stale service.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise DependencyError(f"Binary not found: {command[0]}") from exc


async def spawn_privileged(*command: str, **kwargs) -> asyncio.subprocess.Process:
    """Start a long-lived process as root."""
    return await spawn(*privileged_prefix(), *command, **kwargs)


async def terminate(
    process: asyncio.subprocess.Process | None,
    *,
    timeout: float = 8.0,
    privileged: bool = False,
) -> None:
    """Stop *process* politely, escalating to SIGKILL after *timeout*.

    A privileged child started through ``sudo`` cannot be signalled directly by
    an unprivileged parent, so in that case the signal is delivered with
    ``sudo -n kill``.
    """
    if process is None or process.returncode is not None:
        return

    async def _signal(sig: str) -> None:
        number = getattr(signal, f"SIG{sig}")
        if privileged and not is_root():
            await run(
                *privileged_prefix(), "kill", f"-{sig}", str(process.pid), quiet=True
            )
            return
        try:
            # The group, not just the wrapper: runuser/sudo fork the real child.
            os.killpg(os.getpgid(process.pid), number)
        except (ProcessLookupError, PermissionError, OSError):
            if sig == "TERM":
                process.terminate()
            else:
                process.kill()

    with contextlib.suppress(ProcessLookupError, PrivilegeError):
        await _signal("TERM")
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except TimeoutError:
        log.warning("PID %s ignored SIGTERM; sending SIGKILL", process.pid)

    with contextlib.suppress(ProcessLookupError, PrivilegeError):
        await _signal("KILL")
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=timeout)
