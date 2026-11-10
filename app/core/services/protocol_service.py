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
        self.repository = ProtocolRepository(db)
        self.registry = registry or DecoderRegistry()

    def load(self) -> list[str]:
        """(Re)load user-defined decoders; returns problems found."""
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
        """Register a code-based decoder (e.g. one ported from C)."""
        self.registry.register(decoder)

    def save_definition(self, name: str, decoder_type: str, definition: dict[str, Any], description: str = "") -> None:
        decoder = build_decoder(name, decoder_type, definition)  # validate first
        self.repository.upsert(name, decoder_type, definition, description)
        self.registry.register(decoder)

    def delete_definition(self, name: str) -> None:
        row = self.repository.db.query_one("SELECT id FROM protocols WHERE name = ?", (name,))
        if row:
            self.repository.delete(row["id"])
        self.registry.unregister(name)

    def definitions(self) -> list[dict[str, Any]]:
        return self.repository.list_rows(order_by="name")

    def names(self) -> list[str]:
        return self.registry.names()

    def decode(self, packet: Packet, protocol: str | None = None) -> DecodedMessage | None:
        name = protocol or packet.protocol
        return self.registry.decode(name, packet.data) if name else None
