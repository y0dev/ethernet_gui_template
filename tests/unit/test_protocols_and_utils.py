from __future__ import annotations

import json

import pytest

from app.core.config import AppConfig, ConfigError, load_config, save_config
from app.core.hexutil import format_hex, hexdump, parse_ascii, parse_hex, to_ascii
from app.core.models import Direction, Packet
from app.core.protocols import DeclarativeDecoder, DecoderRegistry, DefinitionError, ProtocolDecoder
from app.core.services import ProtocolService

# A user-style definition used only to exercise the decoder engine.
DEFINITION = {
    "byte_order": "big",
    "fields": [
        {"name": "sync", "type": "uint8_t", "expect": 0xAA, "format": "hex"},
        {"name": "cmd", "type": "uint8_t", "enum": {"1": "READ", "2": "WRITE"},
         "bits": [{"name": "hi", "shift": 4, "width": 4}]},
        {"name": "len", "type": "uint16_t"},
        {"name": "temp", "type": "int16_t", "byte_order": "little"},
        {"name": "body", "type": "bytes", "length_from": "len"},
        {"name": "tail", "type": "ascii"},
    ],
}


def test_declarative_decode():
    """A declarative definition decodes hex, enum, bit, mixed-endian, length-linked and ASCII fields."""
    decoder = DeclarativeDecoder("demo", DEFINITION)
    msg = decoder.decode(b"\xAA\x01\x00\x02\xfe\xff\x10\x20OK")
    values = {f.name: f for f in msg.fields}
    assert values["sync"].text == "0xAA" and values["sync"].valid
    assert values["cmd"].text == "1 (READ)"
    assert values["cmd.hi"].value == 0
    assert values["len"].value == 2
    assert values["temp"].value == -2
    assert values["body"].value == b"\x10\x20" and values["body"].offset == 6
    assert values["tail"].text == "OK"
    assert msg.valid


def test_declarative_reports_problems_without_raising():
    """Bad sync values, short frames and trailing bytes are reported as errors, not raised."""
    decoder = DeclarativeDecoder("demo", DEFINITION)
    bad_sync = decoder.decode(b"\x55\x01\x00\x00\x00\x00")
    assert not bad_sync.valid
    short = decoder.decode(b"\xAA\x01\x00\x09\x00\x00")
    assert short.errors and "needs 9 bytes" in short.errors[0]
    fixed = DeclarativeDecoder("f", {"fields": [{"name": "a", "type": "uint8_t"}]})
    assert "trailing" in fixed.decode(b"\x01\x02").errors[0]


@pytest.mark.parametrize(
    "definition",
    [
        {"fields": []},
        {"fields": [{"name": "a", "type": "long"}]},
        {"fields": [{"name": "a", "type": "uint8_t"}, {"name": "a", "type": "uint8_t"}]},
        {"fields": [{"name": "b", "type": "bytes", "length_from": "missing"}]},
        {"fields": [{"name": "b", "type": "bytes"}, {"name": "c", "type": "uint8_t"}]},
        {"byte_order": "middle", "fields": [{"name": "a", "type": "uint8_t"}]},
    ],
)
def test_invalid_definitions(definition):
    """Invalid decoder definitions are rejected when the decoder is built.

    Args:
        definition: Invalid definition under test.
    """
    with pytest.raises(DefinitionError):
        DeclarativeDecoder("bad", definition)


def test_registry_contains_decoder_crash():
    """An exception inside a decoder becomes a message error, and unknown names give None."""
    class Broken(ProtocolDecoder):
        name = "broken"

        def decode(self, frame):
            """Always fail, to simulate a buggy decoder.

            Args:
                frame: Frame to decode (unused).

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("boom")

    registry = DecoderRegistry()
    registry.register(Broken())
    assert "boom" in registry.decode("broken", b"x").errors[0]
    assert registry.decode("missing", b"x") is None


def test_protocol_service_persists(db):
    """Saved definitions survive a reload, decode packets and can be deleted.

    Args:
        db: Migrated test database.
    """
    service = ProtocolService(db)
    service.save_definition("demo", "declarative", DEFINITION)
    reloaded = ProtocolService(db)
    assert reloaded.load() == []
    packet = Packet(0, Direction.RX, "", "", b"\xAA\x02\x00\x00\x00\x00", protocol="demo")
    assert reloaded.decode(packet).fields[1].text == "2 (WRITE)"
    with pytest.raises(DefinitionError):
        service.save_definition("bad", "declarative", {"fields": []})
    service.delete_definition("demo")
    assert service.names() == []


def test_hex_helpers():
    """The hex and ASCII parsing and formatting helpers handle user input and errors."""
    assert parse_hex("01 02 0a") == b"\x01\x02\x0a"
    assert parse_hex("0x01,0xFF:10-20") == b"\x01\xff\x10\x20"
    with pytest.raises(ValueError):
        parse_hex("123")
    with pytest.raises(ValueError):
        parse_hex("zz")
    assert parse_ascii(r"AT\r\n\x00\\") == b"AT\r\n\x00\\"
    with pytest.raises(ValueError):
        parse_ascii("€")
    assert format_hex(b"\x01\xab") == "01 AB"
    assert to_ascii(b"Hi\x00\x7f") == "Hi.."
    assert hexdump(bytes(range(18))).splitlines()[1].startswith("00000010  10 11")


def test_config_round_trip(tmp_path):
    """Configuration saves and reloads, and invalid files raise ``ConfigError``.

    Args:
        tmp_path: pytest temporary directory.
    """
    path = tmp_path / "cfg.json"
    config = load_config(path)
    assert isinstance(config, AppConfig)
    config.log_level = "DEBUG"
    save_config(config, path)
    assert load_config(path).log_level == "DEBUG"
    path.write_text(json.dumps({"log_level": "LOUD"}))
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(path)
