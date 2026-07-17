from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, TextIO

KEY_CTRL_C = "ctrl_c"
KEY_CANCEL = "c"
KEY_PAUSE = "p"
KEY_QUIT = "q"


class KeySource(Protocol):
    supported: bool

    def poll_key(self) -> str | None: ...


@dataclass(slots=True)
class UnsupportedKeySource:
    reason: str = "unsupported"
    supported: bool = False

    def poll_key(self) -> str | None:
        return None


class PosixKeySource:
    supported = True

    def __init__(self, input_file: TextIO | None = None) -> None:
        self._input_file = input_file or sys.stdin
        self._termios: object | None = None
        self._tty: object | None = None
        self._original_attributes: list[object] | None = None

    def __enter__(self) -> PosixKeySource:
        try:
            import termios
            import tty
        except ImportError:
            self.supported = False
            return self
        self._termios = termios
        self._tty = tty
        try:
            fd = self._input_file.fileno()
            self._original_attributes = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            os.set_blocking(fd, False)
        except OSError:
            self.supported = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._termios is None or self._original_attributes is None:
            return
        try:
            fd = self._input_file.fileno()
            self._termios.tcsetattr(fd, self._termios.TCSADRAIN, self._original_attributes)
            os.set_blocking(fd, True)
        except OSError:
            return

    def poll_key(self) -> str | None:
        if not self.supported:
            return None
        try:
            readable, _writable, _error = select.select([self._input_file], [], [], 0)
            if not readable:
                return None
            key = self._input_file.read(1)
        except OSError:
            return None
        if key == "\x03":
            return KEY_CTRL_C
        return key or None
