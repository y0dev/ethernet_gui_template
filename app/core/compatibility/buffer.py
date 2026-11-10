"""Bounds-checked buffer abstractions replacing C pointer/length idioms.

* :class:`ByteReader` replaces ``const uint8_t *p`` walking a receive buffer.
* :class:`ByteWriter` replaces ``uint8_t buf[N]; size_t len;`` with a fixed
  capacity, so an overflow that would corrupt memory in C raises here.
* :class:`RingBuffer` is a fixed-capacity byte FIFO.  Its full-buffer policy is
  explicit because C ring buffers differ (reject vs. overwrite oldest); match
  the policy of the module being ported.
"""

from __future__ import annotations

from enum import Enum

from .endian import ByteOrder, from_bytes, to_bytes
from .fixed_int import IntType

Buffer = bytes | bytearray | memoryview


class BufferOverrunError(IndexError):
    """A read or write would go past the end of the buffer."""


class ByteReader:
    """Sequential, bounds-checked reader over an immutable view."""

    def __init__(self, data: Buffer, order: ByteOrder = ByteOrder.LITTLE) -> None:
        """Create a reader positioned at the start of ``data``.

        Args:
            data: Buffer to read; it is viewed, not copied.
            order: Default byte order for :meth:`read_int`.
        """
        self._view = memoryview(data).cast("B")
        self._pos = 0
        self.order = order

    @property
    def position(self) -> int:
        """Current read offset in bytes."""
        return self._pos

    @property
    def remaining(self) -> int:
        """Number of unread bytes."""
        return len(self._view) - self._pos

    def seek(self, position: int) -> None:
        """Move the read position to an absolute offset.

        Args:
            position: New offset, from 0 to the buffer length inclusive.

        Raises:
            BufferOverrunError: If ``position`` is outside the buffer.
        """
        if not 0 <= position <= len(self._view):
            raise BufferOverrunError(f"seek to {position} outside buffer of {len(self._view)}")
        self._pos = position

    def skip(self, count: int) -> None:
        """Advance the read position.

        Args:
            count: Number of bytes to skip; may be negative to step back.

        Raises:
            BufferOverrunError: If the new position is outside the buffer.
        """
        self.seek(self._pos + count)

    def read_bytes(self, count: int) -> bytes:
        """Read a fixed number of bytes and advance.

        Args:
            count: Number of bytes to read.

        Returns:
            The bytes read.

        Raises:
            BufferOverrunError: If ``count`` is negative or exceeds ``remaining``.
        """
        if count < 0 or count > self.remaining:
            raise BufferOverrunError(
                f"read of {count} bytes at offset {self._pos}, only {self.remaining} remaining"
            )
        chunk = bytes(self._view[self._pos:self._pos + count])
        self._pos += count
        return chunk

    def read_int(self, ctype: IntType, order: ByteOrder | None = None) -> int:
        """Read one fixed-width integer and advance.

        Args:
            ctype: C integer type to read.
            order: Byte order override; defaults to the reader's order.

        Returns:
            The decoded integer.

        Raises:
            BufferOverrunError: If too few bytes remain.
        """
        return from_bytes(self.read_bytes(ctype.size), ctype, order or self.order)

    def read_rest(self) -> bytes:
        """Read every remaining byte.

        Returns:
            The unread bytes; the reader is left at the end.
        """
        return self.read_bytes(self.remaining)


class ByteWriter:
    """Sequential writer with a hard capacity (``None`` = unbounded)."""

    def __init__(self, capacity: int | None = None, order: ByteOrder = ByteOrder.LITTLE) -> None:
        """Create an empty writer.

        Args:
            capacity: Maximum number of bytes, or None for no limit.
            order: Default byte order for :meth:`write_int`.

        Raises:
            ValueError: If ``capacity`` is negative.
        """
        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0")
        self._buf = bytearray()
        self.capacity = capacity
        self.order = order

    def __len__(self) -> int:
        """Return the number of bytes written so far."""
        return len(self._buf)

    def write_bytes(self, data: Buffer) -> None:
        """Append raw bytes.

        Args:
            data: Bytes to append.

        Raises:
            BufferOverrunError: If the write would exceed ``capacity``.
        """
        if self.capacity is not None and len(self._buf) + len(data) > self.capacity:
            raise BufferOverrunError(
                f"write of {len(data)} bytes exceeds capacity {self.capacity} "
                f"({len(self._buf)} used)"
            )
        self._buf += bytes(data)

    def write_int(self, value: int, ctype: IntType, order: ByteOrder | None = None) -> None:
        """Append one fixed-width integer.

        Args:
            value: Integer to encode; must fit ``ctype``.
            ctype: C integer type to write.
            order: Byte order override; defaults to the writer's order.

        Raises:
            BufferOverrunError: If the write would exceed ``capacity``.
            ValueError: If ``value`` does not fit ``ctype``.
        """
        self.write_bytes(to_bytes(value, ctype, order or self.order))

    def patch(self, offset: int, data: Buffer) -> None:
        """Overwrite bytes that were already written, e.g. to back-fill a length field.

        Args:
            offset: Start of the region to overwrite.
            data: Replacement bytes.

        Raises:
            BufferOverrunError: If the region extends past the written data.
        """
        if offset < 0 or offset + len(data) > len(self._buf):
            raise BufferOverrunError("patch outside written region")
        self._buf[offset:offset + len(data)] = bytes(data)

    def to_bytes(self) -> bytes:
        """Return a copy of everything written.

        Returns:
            The written bytes.
        """
        return bytes(self._buf)


class OverflowPolicy(str, Enum):
    REJECT = "reject"            # write() stores what fits and reports the count
    OVERWRITE_OLDEST = "overwrite_oldest"


class RingBuffer:
    """Fixed-capacity byte FIFO."""

    def __init__(self, capacity: int, policy: OverflowPolicy = OverflowPolicy.REJECT) -> None:
        """Create an empty ring buffer.

        Args:
            capacity: Maximum number of stored bytes.
            policy: What to do when a write does not fit.

        Raises:
            ValueError: If ``capacity`` is not positive.
        """
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._buf = bytearray(capacity)
        self._capacity = capacity
        self._head = 0  # read index
        self._count = 0
        self.policy = policy
        self.dropped = 0  # bytes lost to overwrite or rejection

    @property
    def capacity(self) -> int:
        """Maximum number of bytes the buffer can hold."""
        return self._capacity

    def __len__(self) -> int:
        """Return the number of bytes currently stored."""
        return self._count

    @property
    def free(self) -> int:
        """Number of bytes that can be written before the buffer is full."""
        return self._capacity - self._count

    def write(self, data: Buffer) -> int:
        """Store bytes according to the overflow policy.

        With ``REJECT``, only what fits is stored. With ``OVERWRITE_OLDEST``,
        the oldest bytes are discarded to make room. Lost bytes are added to
        ``dropped``.

        Args:
            data: Bytes to store.

        Returns:
            The number of bytes accepted from ``data``.
        """
        raw = bytes(data)
        if self.policy is OverflowPolicy.REJECT:
            accepted = min(len(raw), self.free)
            self.dropped += len(raw) - accepted
            raw = raw[:accepted]
        else:
            accepted = len(raw)
            if len(raw) > self._capacity:
                self.dropped += len(raw) - self._capacity
                raw = raw[-self._capacity:]
            excess = len(raw) - self.free
            if excess > 0:
                self._head = (self._head + excess) % self._capacity
                self._count -= excess
                self.dropped += excess
        tail = (self._head + self._count) % self._capacity
        first = min(len(raw), self._capacity - tail)
        self._buf[tail:tail + first] = raw[:first]
        self._buf[0:len(raw) - first] = raw[first:]
        self._count += len(raw)
        return accepted

    def peek(self, count: int | None = None) -> bytes:
        """Return the oldest bytes without removing them.

        Args:
            count: Maximum number of bytes, or None for everything stored.

        Returns:
            Up to ``count`` bytes in FIFO order.
        """
        count = self._count if count is None else min(count, self._count)
        first = min(count, self._capacity - self._head)
        return bytes(self._buf[self._head:self._head + first]) + bytes(self._buf[0:count - first])

    def read(self, count: int | None = None) -> bytes:
        """Remove and return the oldest bytes.

        Args:
            count: Maximum number of bytes, or None for everything stored.

        Returns:
            Up to ``count`` bytes in FIFO order.
        """
        chunk = self.peek(count)
        self._head = (self._head + len(chunk)) % self._capacity
        self._count -= len(chunk)
        return chunk

    def clear(self) -> None:
        """Discard all stored bytes. The ``dropped`` counter is kept."""
        self._head = 0
        self._count = 0
