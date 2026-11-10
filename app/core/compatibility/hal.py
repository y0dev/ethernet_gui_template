"""Hardware abstraction interfaces.

Ported logic must not touch hardware or the OS clock directly.  It receives
one of these interfaces instead, so the same code runs against a simulated
device in tests and against a real transport in the application.

Only interfaces and test doubles live here.  Concrete implementations are
added when a C module that needs them is ported.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """Replacement for tick counters / ``HAL_GetTick()`` style functions."""

    @abstractmethod
    def monotonic_ms(self) -> int:
        """Milliseconds from an arbitrary fixed origin; never goes backwards."""


class SystemClock(Clock):
    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


class ManualClock(Clock):
    """Deterministic clock for tests of timing-dependent code."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def monotonic_ms(self) -> int:
        return self._now

    def advance(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += milliseconds


class RegisterBus(ABC):
    """Replacement for memory-mapped or bus register access."""

    @abstractmethod
    def read(self, address: int, length: int) -> bytes:
        """Read ``length`` bytes starting at ``address``."""

    @abstractmethod
    def write(self, address: int, data: bytes) -> None:
        """Write ``data`` starting at ``address``."""


class MemoryRegisterBus(RegisterBus):
    """In-memory register space for tests; rejects out-of-range access."""

    def __init__(self, size: int) -> None:
        self._mem = bytearray(size)

    def read(self, address: int, length: int) -> bytes:
        self._check(address, length)
        return bytes(self._mem[address:address + length])

    def write(self, address: int, data: bytes) -> None:
        self._check(address, len(data))
        self._mem[address:address + len(data)] = data

    def _check(self, address: int, length: int) -> None:
        if address < 0 or length < 0 or address + length > len(self._mem):
            raise IndexError(f"register access [{address:#x}, +{length}] outside {len(self._mem)} bytes")


class ByteTransport(ABC):
    """Replacement for a blocking UART/socket write used by ported C code."""

    @abstractmethod
    def write(self, data: bytes) -> int:
        """Queue bytes for transmission; returns the number accepted."""
