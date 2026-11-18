"""Output-only terminal integration: leave input, scrollback and search to Ghostty."""

from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import struct
import termios
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TextIO

# A Markdown document must not be able to inject terminal commands. Sanitize both
# before parsing and after entity decoding (e.g. &#27; in a code span).
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
RGB = tuple[int, int, int]
Palette = tuple[RGB, RGB, RGB]
_PENDING_INPUT = bytearray()


def pending_input() -> bytes:
    """Keyboard bytes arriving during terminal queries belong to the reader."""
    result = bytes(_PENDING_INPUT)
    _PENDING_INPUT.clear()
    return result


@dataclass(frozen=True)
class TerminalTheme:
    palette: Palette
    foreground: RGB
    background: RGB


COLOR_REPLY = re.compile(
    rb"\x1b\](4;[246]|10|11);rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/"
    rb"([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)"
)


def read_palette(fd: int, timeout: float = 0.3) -> TerminalTheme | None:
    """Query three ANSI accents without changing the terminal's palette.

    Read from the controlling TTY, never Markdown on stdin. Restore input flags
    even on interruption, and avoid a query when keyboard input is already queued.
    """
    original = termios.tcgetattr(fd)
    settings = termios.tcgetattr(fd)
    settings[3] &= ~(termios.ICANON | termios.ECHO)
    settings[6][termios.VMIN] = 0
    settings[6][termios.VTIME] = 0
    data = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        if select.select([fd], [], [], 0)[0]:
            return None
        os.write(fd, b"\x1b]4;2;?;6;?;4;?\x1b\\\x1b]10;?\x1b\\\x1b]11;?\x1b\\")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(data) < 4096:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(fd, 1)
            if not chunk:
                break
            data += chunk
            colors = {}
            for match in COLOR_REPLY.finditer(data):
                colors[match[1]] = tuple(
                    round(int(part, 16) * 255 / (16 ** len(part) - 1))
                    for part in match.groups()[1:]
                )
            if len(colors) == 5:
                return TerminalTheme(
                    (colors[b"4;2"], colors[b"4;6"], colors[b"4;4"]),
                    colors[b"10"],
                    colors[b"11"],
                )
    finally:
        _PENDING_INPUT.extend(COLOR_REPLY.sub(b"", data))
        termios.tcsetattr(fd, termios.TCSANOW, original)
    return None


def sync(fd: int, timeout: float = 0.25) -> bool:
    """Wait until the terminal has parsed everything written before this call.

    A Device Status Report is answered in order, so its reply cannot arrive
    until the emulator has consumed the bytes that preceded it. Printing a long
    document leaves Ghostty parsing for a while, and a title set at the end of
    it is not visible to AppleScript until that backlog clears.
    """
    original = termios.tcgetattr(fd)
    settings = termios.tcgetattr(fd)
    settings[3] &= ~(termios.ICANON | termios.ECHO)
    settings[6][termios.VMIN] = 0
    settings[6][termios.VTIME] = 0
    data = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        os.write(fd, b"\x1b[5n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(data) < 256:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            data += chunk
            if b"\x1b[0n" in data:
                return True
    except OSError:
        return False
    finally:
        _PENDING_INPUT.extend(data.replace(b"\x1b[0n", b""))
        termios.tcsetattr(fd, termios.TCSANOW, original)
    return False


def query_palette() -> TerminalTheme | None:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            return read_palette(fd)
        finally:
            os.close(fd)
    except (OSError, termios.error):
        return None


def clean_text(text: str) -> str:
    return CONTROLS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass(frozen=True)
class Terminal:
    columns: int = 80
    rows: int = 24
    cell_width: int = 8
    cell_height: int = 17
    is_tty: bool = False
    graphics: bool = False

    @classmethod
    def detect(cls, stream: TextIO, env: Mapping[str, str] | None = None) -> Terminal:
        env = os.environ if env is None else env
        is_tty = stream.isatty()
        size = shutil.get_terminal_size((80, 24))
        columns, rows, cw, ch = size.columns, size.lines, 8, 17
        if is_tty:
            try:
                r, c, x, y = struct.unpack(
                    "HHHH", fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, bytes(8))
                )
                columns, rows = c or columns, r or rows
                if x and y and c and r:
                    cw, ch = max(1, x // c), max(1, y // r)
            except (OSError, ValueError):
                pass
        supported = env.get("TERM_PROGRAM") in {"ghostty", "kitty"} or any(
            name in env.get("TERM", "") for name in ("ghostty", "kitty")
        )
        multiplexed = any(env.get(key) for key in ("TMUX", "STY", "ZELLIJ"))
        return cls(columns, rows, cw, ch, is_tty, is_tty and supported and not multiplexed)


CELL_REPLY = re.compile(rb"\x1b\[6;(\d+);(\d+)t")
GRAPHICS_REPLY = re.compile(rb"\x1b_Gi=2147483647;([^\x1b]*)\x1b\\")


def read_terminal_info(
    fd: int,
    *,
    probe: bool = True,
    timeout: float = 0.2,
) -> tuple[tuple[int, int] | None, bool | None]:
    """Query physical cells through the TTY, including when SSH lost PTY pixels."""
    original = termios.tcgetattr(fd)
    settings = termios.tcgetattr(fd)
    settings[3] &= ~(termios.ICANON | termios.ECHO)
    settings[6][termios.VMIN] = settings[6][termios.VTIME] = 0
    data = b""
    cell, graphics = None, None
    try:
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        if select.select([fd], [], [], 0)[0]:
            return cell, graphics
        request = b"\x1b[16t"
        if probe:
            request += b"\x1b_Gi=2147483647,a=q,t=d,f=24,s=1,v=1;AAAA\x1b\\"
        os.write(fd, request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(data) < 4096:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(fd, 1)
            if not chunk:
                break
            data += chunk
            match = CELL_REPLY.search(data)
            if match:
                height, width = map(int, match.groups())
                if 0 < width <= 1000 and 0 < height <= 1000:
                    cell = width, height
            match = GRAPHICS_REPLY.search(data)
            if match:
                graphics = match[1] == b"OK"
            if cell and (not probe or graphics is not None):
                break
    finally:
        _PENDING_INPUT.extend(GRAPHICS_REPLY.sub(b"", CELL_REPLY.sub(b"", data)))
        termios.tcsetattr(fd, termios.TCSANOW, original)
    return cell, graphics


def refresh_terminal(terminal: Terminal, *, probe: bool = False) -> Terminal:
    if not terminal.is_tty or any(os.environ.get(key) for key in ("TMUX", "STY", "ZELLIJ")):
        return terminal
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            cell, graphics = read_terminal_info(fd, probe=probe)
        finally:
            os.close(fd)
    except (OSError, termios.error):
        return terminal
    return replace(
        terminal,
        cell_width=cell[0] if cell else terminal.cell_width,
        cell_height=cell[1] if cell else terminal.cell_height,
        graphics=graphics if graphics is not None else terminal.graphics,
    )
