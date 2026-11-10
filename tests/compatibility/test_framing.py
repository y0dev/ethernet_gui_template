from __future__ import annotations

import pytest

from app.core.compatibility.endian import ByteOrder
from app.core.compatibility.framing import (
    DelimiterFramer,
    FixedLengthFramer,
    FramingError,
    LengthPrefixedFramer,
    RawFramer,
    create_framer,
)


def feed_bytewise(framer, data: bytes) -> list[bytes]:
    """Feed data to a framer one byte at a time.

    Args:
        framer: Framer under test.
        data: Stream bytes.

    Returns:
        Every frame produced.
    """
    frames = []
    for i in range(len(data)):
        frames += framer.feed(data[i:i + 1])
    return frames


def test_raw_passthrough():
    """Raw framing passes each chunk through and ignores empty chunks."""
    assert RawFramer().feed(b"abc") == [b"abc"]
    assert RawFramer().feed(b"") == []


def test_delimiter_split_across_chunks():
    """A delimiter split across chunks still yields whole frames."""
    framer = DelimiterFramer(b"\r\n")
    assert framer.feed(b"hel") == []
    assert framer.feed(b"lo\r") == []
    assert framer.feed(b"\nwor") == [b"hello\r\n"]
    assert framer.pending == 3
    assert framer.feed(b"ld\r\nx\r\n") == [b"world\r\n", b"x\r\n"]
    assert framer.encode(b"ping") == b"ping\r\n"


def test_delimiter_overflow_resets():
    """A frame longer than the maximum raises and clears the buffer."""
    framer = DelimiterFramer(b"\n", max_frame_length=4)
    with pytest.raises(FramingError):
        framer.feed(b"abcdef")
    assert framer.pending == 0


def test_fixed_length():
    """Fixed-length framing splits a byte stream and rejects wrong-size payloads."""
    framer = FixedLengthFramer(3)
    assert feed_bytewise(framer, b"abcdefg") == [b"abc", b"def"]
    assert framer.pending == 1
    with pytest.raises(FramingError):
        framer.encode(b"ab")


def test_length_prefixed_basic_and_bytewise():
    """Length-prefixed frames round-trip, including when fed byte by byte."""
    framer = LengthPrefixedFramer(2, ByteOrder.BIG)
    stream = framer.encode(b"hello") + framer.encode(b"") + framer.encode(b"xy")
    assert stream[:2] == b"\x00\x05"
    assert feed_bytewise(LengthPrefixedFramer(2, ByteOrder.BIG), stream) == [
        b"\x00\x05hello", b"\x00\x00", b"\x00\x02xy",
    ]


def test_length_prefixed_offset_and_adjust():
    """A length field after a sync byte, uncounted trailers and whole-frame lengths work."""
    # [sync:1][len:1 counting payload only][payload][crc:2 not counted] -> adjust +2
    frame = b"\xAA\x03abc\x12\x34"
    framer =LengthPrefixedFramer(1, ByteOrder.BIG, length_offset=1, length_adjust=2)
    assert framer.feed(frame + frame[:3]) == [frame]
    assert framer.pending == 3
    whole = LengthPrefixedFramer(2, ByteOrder.LITTLE, length_adjust=-2)
    assert whole.feed(b"\x05\x00abc") == [b"\x05\x00abc"]
    with pytest.raises(FramingError):
        framer.encode(b"x")  # length_offset != 0


def test_length_prefixed_rejects_bad_lengths():
    """Oversized, undersized and unencodable lengths raise ``FramingError``."""
    with pytest.raises(FramingError):
        LengthPrefixedFramer(2, max_frame_length=10).feed(b"\x00\x20")
    with pytest.raises(FramingError):
        LengthPrefixedFramer(2, length_adjust=-5).feed(b"\x00\x01")
    with pytest.raises(FramingError):
        LengthPrefixedFramer(1).encode(b"x" * 256)


def test_create_framer():
    """``create_framer`` builds each framer type and rejects bad configurations."""
    assert isinstance(create_framer(None), RawFramer)
    assert isinstance(create_framer({"type": "delimiter", "delimiter_hex": "0a"}), DelimiterFramer)
    lp = create_framer({"type": "length_prefixed", "length_size": 4, "byte_order": "little"})
    assert isinstance(lp, LengthPrefixedFramer) and lp.header_size == 4
    with pytest.raises(ValueError):
        create_framer({"type": "fixed"})
    with pytest.raises(ValueError):
        create_framer({"type": "bogus"})


def test_unwrap_inverts_encode():
    """``unwrap`` removes exactly the framing that ``encode`` adds."""
    for framer in (RawFramer(), DelimiterFramer(b"\r\n"), LengthPrefixedFramer(2),
                   LengthPrefixedFramer(1, length_adjust=-1)):
        assert framer.unwrap(framer.encode(b"abc")) == b"abc"
    trailer = LengthPrefixedFramer(1, ByteOrder.BIG, length_offset=1, length_adjust=2)
    assert trailer.unwrap(b"\xAA\x03abc\x12\x34") == b"abc"
