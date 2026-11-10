"""Parametric CRC engine and simple checksums.

The CRC engine follows the Rocksoft/Williams model (width, poly, init, refin,
refout, xorout).  A C implementation's CRC is reproduced by supplying *its*
parameters - read them from the C source or table, do not pick the catalogue
entry that "looks right".  Every catalogue entry below is a published standard
algorithm and is tested against its published check value (CRC of the ASCII
string ``123456789``).

No project-specific CRC is defined here.  When a C module is ported, add its
parameters next to the ported module and verify against the C code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bits import reverse_bits

Buffer = bytes | bytearray | memoryview


@dataclass(frozen=True)
class CrcParams:
    """Rocksoft model parameters for one CRC algorithm."""

    name: str
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int
    check: int | None = None  # CRC of b"123456789", when published


@dataclass
class Crc:
    """Table-driven CRC for widths 8..64.

    Supports incremental use::

        crc = Crc(CRC16_CCITT_FALSE)
        state = crc.start()
        state = crc.update(state, chunk1)
        state = crc.update(state, chunk2)
        value = crc.finish(state)
    """

    params: CrcParams
    _table: list[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the CRC width and precompute the lookup table.

        Raises:
            ValueError: If the width is not between 8 and 64 bits.
        """
        p = self.params
        if not 8 <= p.width <= 64:
            raise ValueError("CRC width must be between 8 and 64 bits")
        self._mask = (1 << p.width) - 1
        self._table = self._build_table()

    def _build_table(self) -> list[int]:
        """Build the 256-entry lookup table for the configured polynomial.

        Returns:
            The table, in reflected form when ``refin`` is set.
        """
        p = self.params
        table = []
        if p.refin:
            poly = reverse_bits(p.poly, p.width)
            for byte in range(256):
                reg = byte
                for _ in range(8):
                    reg = (reg >> 1) ^ poly if reg & 1 else reg >> 1
                table.append(reg)
        else:
            top = 1 << (p.width - 1)
            for byte in range(256):
                reg = byte << (p.width - 8)
                for _ in range(8):
                    reg = ((reg << 1) ^ p.poly) if reg & top else reg << 1
                table.append(reg & self._mask)
        return table

    def start(self) -> int:
        """Return the initial register state for an incremental calculation.

        Returns:
            The ``init`` value, reflected when ``refin`` is set.
        """
        p = self.params
        return reverse_bits(p.init, p.width) if p.refin else p.init

    def update(self, state: int, data: Buffer) -> int:
        """Feed more bytes into a running CRC calculation.

        Args:
            state: Register state from :meth:`start` or an earlier update.
            data: Bytes to add.

        Returns:
            The new register state.
        """
        table = self._table
        if self.params.refin:
            for byte in bytes(data):
                state = (state >> 8) ^ table[(state ^ byte) & 0xFF]
        else:
            shift = self.params.width - 8
            mask = self._mask
            for byte in bytes(data):
                state = ((state << 8) & mask) ^ table[((state >> shift) ^ byte) & 0xFF]
        return state

    def finish(self, state: int) -> int:
        """Turn a register state into the final CRC value.

        Applies output reflection and ``xorout``.

        Args:
            state: Register state after the last :meth:`update`.

        Returns:
            The CRC value.
        """
        p = self.params
        if p.refin != p.refout:
            state = reverse_bits(state, p.width)
        return (state ^ p.xorout) & self._mask

    def compute(self, data: Buffer) -> int:
        """Compute the CRC of a complete buffer in one call.

        Args:
            data: Bytes to checksum.

        Returns:
            The CRC value.
        """
        return self.finish(self.update(self.start(), data))


# Published standard algorithms (parameters and check values from the
# "Catalogue of parametrised CRC algorithms", reveng.sourceforge.io).
CRC8 = CrcParams("CRC-8/SMBUS", 8, 0x07, 0x00, False, False, 0x00, 0xF4)
CRC8_MAXIM = CrcParams("CRC-8/MAXIM-DOW", 8, 0x31, 0x00, True, True, 0x00, 0xA1)
CRC16_CCITT_FALSE = CrcParams("CRC-16/IBM-3740", 16, 0x1021, 0xFFFF, False, False, 0x0000, 0x29B1)
CRC16_XMODEM = CrcParams("CRC-16/XMODEM", 16, 0x1021, 0x0000, False, False, 0x0000, 0x31C3)
CRC16_KERMIT = CrcParams("CRC-16/KERMIT", 16, 0x1021, 0x0000, True, True, 0x0000, 0x2189)
CRC16_ARC = CrcParams("CRC-16/ARC", 16, 0x8005, 0x0000, True, True, 0x0000, 0xBB3D)
CRC16_MODBUS = CrcParams("CRC-16/MODBUS", 16, 0x8005, 0xFFFF, True, True, 0x0000, 0x4B37)
CRC32 = CrcParams("CRC-32/ISO-HDLC", 32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF, 0xCBF43926)
CRC32_BZIP2 = CrcParams("CRC-32/BZIP2", 32, 0x04C11DB7, 0xFFFFFFFF, False, False, 0xFFFFFFFF, 0xFC891918)
CRC32_MPEG2 = CrcParams("CRC-32/MPEG-2", 32, 0x04C11DB7, 0xFFFFFFFF, False, False, 0x00000000, 0x0376E6E7)
CRC32C = CrcParams("CRC-32/ISCSI", 32, 0x1EDC6F41, 0xFFFFFFFF, True, True, 0xFFFFFFFF, 0xE3069283)

CATALOGUE: dict[str, CrcParams] = {
    p.name: p
    for p in (
        CRC8, CRC8_MAXIM, CRC16_CCITT_FALSE, CRC16_XMODEM, CRC16_KERMIT,
        CRC16_ARC, CRC16_MODBUS, CRC32, CRC32_BZIP2, CRC32_MPEG2, CRC32C,
    )
}


# -- simple checksums ---------------------------------------------------------
def sum8(data: Buffer) -> int:
    """Compute the modulo-256 sum of all bytes.

    Args:
        data: Bytes to sum.

    Returns:
        The 8-bit sum.
    """
    return sum(bytes(data)) & 0xFF


def sum8_twos_complement(data: Buffer) -> int:
    """Compute the checksum byte that makes the modulo-256 sum of data plus checksum zero.

    Args:
        data: Bytes to protect.

    Returns:
        The two's-complement checksum byte.
    """
    return (-sum(bytes(data))) & 0xFF


def xor8(data: Buffer) -> int:
    """XOR all bytes together, giving a longitudinal redundancy check (LRC).

    Args:
        data: Bytes to combine.

    Returns:
        The 8-bit XOR of all bytes.
    """
    result = 0
    for byte in bytes(data):
        result ^= byte
    return result


def sum16(data: Buffer) -> int:
    """Compute the modulo-65536 sum of all bytes.

    Args:
        data: Bytes to sum.

    Returns:
        The 16-bit sum.
    """
    return sum(bytes(data)) & 0xFFFF


def fletcher16(data: Buffer) -> int:
    """Compute the Fletcher-16 checksum (modulo 255).

    Args:
        data: Bytes to checksum.

    Returns:
        ``(sum2 << 8) | sum1``.
    """
    sum1 = sum2 = 0
    for byte in bytes(data):
        sum1 = (sum1 + byte) % 255
        sum2 = (sum2 + sum1) % 255
    return (sum2 << 8) | sum1


def internet_checksum(data: Buffer) -> int:
    """Compute the RFC 1071 Internet checksum.

    This is the one's-complement sum of big-endian 16-bit words. An odd
    trailing byte is padded with zero.

    Args:
        data: Bytes to checksum.

    Returns:
        The 16-bit checksum.
    """
    raw = bytes(data)
    if len(raw) % 2:
        raw += b"\x00"
    total = 0
    for i in range(0, len(raw), 2):
        total += (raw[i] << 8) | raw[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF
