"""Native file chooser integration.

NEXTRON is a terminal application, so it does not draw a file browser of its
own: it asks the desktop for the *real* one (zenity / kdialog / yad / qarma),
which supports multiple selection, bookmarks and typing paths -- everything a
user already knows.

If no chooser is installed, or there is no graphical session (a plain TTY, SSH
without X forwarding), :func:`available` reports why and the caller falls back
to typing a path. Nothing here is required for NEXTRON to work.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from nextron.core import constants, process

log = logging.getLogger(__name__)

__all__ = [
    "BLOCKLIST_FILTER",
    "VPN_FILTER",
    "DialogResult",
    "FileFilter",
    "available",
    "default_start_dir",
    "pick_files",
    "unavailable_reason",
]

#: How long to wait for the user to finish choosing files.
DIALOG_TIMEOUT = 600.0


@dataclass(frozen=True, slots=True)
class DialogResult:
    """The outcome of a chooser request.

    ``cancelled`` and ``error`` are kept apart on purpose: closing the dialog
    is normal and needs no message, while a chooser that could not open (no
    display under ``sudo``, for instance) must be reported so the user knows
    to type a path instead.
    """

    paths: tuple[Path, ...] = ()
    cancelled: bool = False
    error: str | None = None

    def __bool__(self) -> bool:
        return bool(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def __iter__(self):
        return iter(self.paths)


@dataclass(frozen=True, slots=True)
class FileFilter:
    """A named set of glob patterns offered in the chooser."""

    name: str
    patterns: tuple[str, ...]

    @property
    def zenity(self) -> str:
        """``"VPN profiles | *.ovpn *.conf"`` -- zenity/yad/qarma syntax."""
        return f"{self.name} | {' '.join(self.patterns)}"

    @property
    def kdialog(self) -> str:
        """``"*.ovpn *.conf|VPN profiles"`` -- kdialog syntax."""
        return f"{' '.join(self.patterns)}|{self.name}"


def _patterns(suffixes: tuple[str, ...]) -> tuple[str, ...]:
    """``(".ovpn",)`` -> ``("*.ovpn", "*.OVPN")`` so case never hides a file."""
    globs: list[str] = []
    for suffix in suffixes:
        globs.append(f"*{suffix}")
        globs.append(f"*{suffix.upper()}")
    return tuple(globs)


VPN_FILTER = FileFilter("VPN profiles", _patterns(constants.VPN_IMPORT_SUFFIXES))
BLOCKLIST_FILTER = FileFilter("Blocklists", _patterns(constants.BLOCKLIST_SUFFIXES))
ALL_FILTER = FileFilter("All files", ("*",))

#: Backends in preference order. zenity and its clones share one command line.
_ZENITY_LIKE = ("zenity", "qarma", "matedialog")


def _graphical_session() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def available() -> str | None:
    """Return the name of a usable chooser backend, or ``None``."""
    if os.environ.get("NEXTRON_NO_FILE_DIALOG"):
        return None
    if not _graphical_session():
        return None
    for binary in (*_ZENITY_LIKE, "yad", "kdialog"):
        if process.which(binary):
            return binary
    return None


def unavailable_reason() -> str:
    """Explain, in one line, why the native chooser cannot be used."""
    if os.environ.get("NEXTRON_NO_FILE_DIALOG"):
        return "disabled by NEXTRON_NO_FILE_DIALOG"
    if not _graphical_session():
        return "no graphical session (DISPLAY/WAYLAND_DISPLAY is unset)"
    return "no file chooser installed (try: sudo apt install zenity)"


def _build_command(
    backend: str,
    *,
    title: str,
    file_filter: FileFilter,
    multiple: bool,
    start_dir: Path | None,
) -> list[str]:
    binary = process.which(backend) or backend

    if backend in _ZENITY_LIKE:
        command = [
            binary,
            "--file-selection",
            f"--title={title}",
            "--separator=\n",
            f"--file-filter={file_filter.zenity}",
            f"--file-filter={ALL_FILTER.zenity}",
        ]
        if multiple:
            command.append("--multiple")
        if start_dir is not None:
            command.append(f"--filename={start_dir}/")
        return command

    if backend == "yad":
        command = [
            binary,
            "--file",
            f"--title={title}",
            "--separator=\n",
            f"--file-filter={file_filter.zenity}",
            f"--file-filter={ALL_FILTER.zenity}",
        ]
        if multiple:
            command.append("--multiple")
        if start_dir is not None:
            command.append(f"--filename={start_dir}/")
        return command

    # kdialog: positional start directory, then a single filter string.
    command = [
        binary,
        "--title",
        title,
        "--getopenfilename",
        str(start_dir) if start_dir is not None else str(Path.home()),
        f"{file_filter.kdialog}\n{ALL_FILTER.kdialog}",
    ]
    if multiple:
        command.append("--multiple")
    return command


def _parse_output(backend: str, raw: str) -> list[Path]:
    """Turn a backend's stdout into a list of existing paths."""
    if not raw.strip():
        return []

    if backend == "kdialog":
        # kdialog prints space-separated, shell-quoted paths.
        tokens = shlex.split(raw.strip())
    else:
        tokens = [line for line in raw.splitlines() if line.strip()]

    paths: list[Path] = []
    for token in tokens:
        candidate = Path(token.strip().strip("'\"")).expanduser()
        if candidate.is_file() and candidate not in paths:
            paths.append(candidate)
        elif not candidate.is_file():
            log.debug("Chooser returned a non-file entry: %s", token)
    return paths


def default_start_dir() -> Path:
    """Open where downloads usually land, falling back to the home directory."""
    for candidate in (Path.home() / "Downloads", Path.home() / "downloads"):
        if candidate.is_dir():
            return candidate
    return Path.home()


async def pick_files(
    *,
    title: str = "Select files",
    file_filter: FileFilter = ALL_FILTER,
    multiple: bool = True,
    start_dir: Path | None = None,
) -> DialogResult:
    """Open the desktop's file chooser and report what came back."""
    backend = available()
    if backend is None:
        reason = unavailable_reason()
        log.info("Native file chooser unavailable: %s", reason)
        return DialogResult(error=reason)

    command = _build_command(
        backend,
        title=title,
        file_filter=file_filter,
        multiple=multiple,
        start_dir=start_dir if start_dir is not None else default_start_dir(),
    )
    log.debug("Opening %s file chooser", backend)
    result = await process.run(*command, timeout=DIALOG_TIMEOUT)

    if result.timed_out:
        return DialogResult(
            error=f"the chooser did not respond within {DIALOG_TIMEOUT:.0f}s"
        )

    selected = _parse_output(backend, result.stdout)
    if selected:
        log.info("File chooser returned %d file(s)", len(selected))
        return DialogResult(paths=tuple(selected))

    # No selection: either the user closed the dialog, or it never opened.
    failure = _display_failure(result.stderr)
    if failure is not None:
        log.warning("File chooser failed: %s", failure)
        return DialogResult(error=failure)

    log.debug("File chooser closed without a selection (rc=%s)", result.returncode)
    return DialogResult(cancelled=True)


#: Strings that mean "the dialog could not be shown", not "the user cancelled".
_DISPLAY_ERRORS = (
    "cannot open display",
    "could not open display",
    "unable to init server",
    "no protocol specified",
    "authorization required",
    "failed to connect to",
    "gtk_init",
)


def _display_failure(stderr: str) -> str | None:
    """Detect a chooser that could not reach the display."""
    lowered = stderr.lower()
    for marker in _DISPLAY_ERRORS:
        if marker in lowered:
            return (
                "the file chooser could not reach the desktop session "
                "(common when running under sudo) -- type a path instead"
            )
    return None
