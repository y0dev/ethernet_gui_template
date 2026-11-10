"""Loads protocol decoders from the database and decodes packets."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..database import Database, ProtocolRepository
from ..models import Packet
from ..protocols import DecodedMessage, DecoderRegistry, DefinitionError, ProtocolDecoder, build_decoder

log = logging.getLogger(__name__)


class ProtocolService:
    def __init__(self, db: Database, registry: DecoderRegistry | None = None) -> None:
        """Create the service.

        Args:
            db: Shared database holding protocol definitions.
            registry: Decoder registry to fill; a new one is created by default.
        """
        self.repository = ProtocolRepository(db)
        self.registry = registry or DecoderRegistry()

    def load(self) -> list[str]:
        """Load or reload every user-defined decoder from the database.

        Invalid definitions are skipped and logged.

        Returns:
            One problem description per rejected definition.
        """
        problems = []
        for row in self.repository.list_rows(order_by="name"):
            try:
                decoder = build_decoder(row["name"], row["decoder_type"], json.loads(row["definition_json"]))
            except (DefinitionError, json.JSONDecodeError) as exc:
                problems.append(f"{row['name']}: {exc}")
                log.warning("protocol definition rejected", extra={"context": {"name": row["name"], "error": str(exc)}})
                continue
            self.registry.register(decoder)
        return problems

    def register(self, decoder: ProtocolDecoder) -> None:
        """Register a code-based decoder, such as one ported from C.

        Args:
            decoder: Decoder to register under its own name.
        """
        self.registry.register(decoder)

    def save_definition(self, name: str, decoder_type: str, definition: dict[str, Any], description: str = "") -> None:
        """Validate, store and register a decoder definition.

        Args:
            name: Protocol name.
            decoder_type: Decoder kind, e.g. ``"declarative"``.
            definition: Decoder definition.
            description: Human-readable description.

        Raises:
            DefinitionError: If the definition is invalid; nothing is stored.
        """
        decoder = build_decoder(name, decoder_type, definition)  # validate first
        self.repository.upsert(name, decoder_type, definition, description)
        self.registry.register(decoder)

    def delete_definition(self, name: str) -> None:
        """Delete a stored definition and unregister its decoder.

        Args:
            name: Protocol name.
        """
        row = self.repository.db.query_one("SELECT id FROM protocols WHERE name = ?", (name,))
        if row:
            self.repository.delete(row["id"])
        self.registry.unregister(name)

    def definitions(self) -> list[dict[str, Any]]:
        """List the stored protocol definitions.

        Returns:
            Protocol rows ordered by name.
        """
        return self.repository.list_rows(order_by="name")

    def names(self) -> list[str]:
        """List the names of every registered decoder.

        Returns:
            The decoder names.
        """
        return self.registry.names()

    def decode(self, packet: Packet, protocol: str | None = None) -> DecodedMessage | None:
        """Decode a packet with a named protocol or the packet's own protocol.

        Args:
            packet: Packet to decode.
            protocol: Protocol name override; defaults to ``packet.protocol``.

        Returns:
            The decoded message, or None if no protocol name is set or no
            decoder is registered for it.
        """
        name = protocol or packet.protocol
        return self.registry.decode(name, packet.data) if name else None
