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
        """Return the time in milliseconds since an arbitrary fixed origin.

        Returns:
            A millisecond count that never goes backwards.
        """


class SystemClock(Clock):
    def monotonic_ms(self) -> int:
        """Return the host's monotonic clock in milliseconds.

        Returns:
            Milliseconds from ``time.monotonic_ns``.
        """
        return time.monotonic_ns() // 1_000_000


class ManualClock(Clock):
    """Deterministic clock for tests of timing-dependent code."""

    def __init__(self, start_ms: int = 0) -> None:
        """Create a clock stopped at a chosen time.

        Args:
            start_ms: Initial reading in milliseconds.
        """
        self._now = start_ms

    def monotonic_ms(self) -> int:
        """Return the current manual reading.

        Returns:
            The time in milliseconds.
        """
        return self._now

    def advance(self, milliseconds: int) -> None:
        """Move the clock forward.

        Args:
            milliseconds: Amount to advance; must not be negative.

        Raises:
            ValueError: If ``milliseconds`` is negative.
        """
        if milliseconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += milliseconds


class RegisterBus(ABC):
    """Replacement for memory-mapped or bus register access."""

    @abstractmethod
    def read(self, address: int, length: int) -> bytes:
        """Read a block of registers.

        Args:
            address: First register address.
            length: Number of bytes to read.

        Returns:
            The bytes read.
        """

    @abstractmethod
    def write(self, address: int, data: bytes) -> None:
        """Write a block of registers.

        Args:
            address: First register address.
            data: Bytes to write.
        """


class MemoryRegisterBus(RegisterBus):
    """In-memory register space for tests; rejects out-of-range access."""

    def __init__(self, size: int) -> None:
        """Create a zero-filled register space.

        Args:
            size: Size of the address space in bytes.
        """
        self._mem = bytearray(size)

    def read(self, address: int, length: int) -> bytes:
        """Read a block of the in-memory register space.

        Args:
            address: First register address.
            length: Number of bytes to read.

        Returns:
            The bytes read.

        Raises:
            IndexError: If the access is outside the register space.
        """
        self._check_bounds(address, length)
        return bytes(self._mem[address:address + length])

    def write(self, address: int, data: bytes) -> None:
        """Write a block of the in-memory register space.

        Args:
            address: First register address.
            data: Bytes to write.

        Raises:
            IndexError: If the access is outside the register space.
        """
        self._check_bounds(address, len(data))
        self._mem[address:address + len(data)] = data

    def _check_bounds(self, address: int, length: int) -> None:
        """Reject accesses that fall outside the register space.

        Args:
            address: First register address.
            length: Number of bytes accessed.

        Raises:
            IndexError: If the range is negative or extends past the end.
        """
        if address < 0 or length < 0 or address + length > len(self._mem):
            raise IndexError(f"register access [{address:#x}, +{length}] outside {len(self._mem)} bytes")


class ByteTransport(ABC):
    """Replacement for a blocking UART/socket write used by ported C code."""

    @abstractmethod
    def write(self, data: bytes) -> int:
        """Queue bytes for transmission.

        Args:
            data: Bytes to send.

        Returns:
            The number of bytes accepted.
        """
