"""Device inventory management."""

from __future__ import annotations

import logging

from ..database import Database, DeviceRepository
from ..models import Device

log = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, db: Database) -> None:
        self.repository = DeviceRepository(db)

    @staticmethod
    def validate(device: Device) -> None:
        if not device.name.strip():
            raise ValueError("device name is required")
        if not 0 <= device.port <= 65535:
            raise ValueError("port must be between 0 and 65535")

    def list(self) -> list[Device]:
        return self.repository.list()

    def get(self, device_id: int) -> Device | None:
        return self.repository.get(device_id)

    def save(self, device: Device) -> Device:
        self.validate(device)
        if device.id is None:
            self.repository.add(device)
            log.info("device added", extra={"context": {"id": device.id, "name": device.name}})
        else:
            self.repository.update(device)
            log.info("device updated", extra={"context": {"id": device.id, "name": device.name}})
        return device

    def delete(self, device_id: int) -> None:
        self.repository.delete(device_id)
        log.info("device deleted", extra={"context": {"id": device_id}})
