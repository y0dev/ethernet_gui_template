"""Device inventory management."""

from __future__ import annotations

import logging

from ..database import Database, DeviceRepository
from ..models import Device

log = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, db: Database) -> None:
        """Create the service on top of the device repository.

        Args:
            db: Shared database.
        """
        self.repository = DeviceRepository(db)

    @staticmethod
    def validate(device: Device) -> None:
        """Check that a device can be saved.

        Args:
            device: Device to check.

        Raises:
            ValueError: If the name is blank or the port is out of range.
        """
        if not device.name.strip():
            raise ValueError("device name is required")
        if not 0 <= device.port <= 65535:
            raise ValueError("port must be between 0 and 65535")

    def list(self) -> list[Device]:
        """List every registered device.

        Returns:
            The devices ordered by name.
        """
        return self.repository.list()

    def get(self, device_id: int) -> Device | None:
        """Fetch one device.

        Args:
            device_id: Device id.

        Returns:
            The device, or None if it does not exist.
        """
        return self.repository.get(device_id)

    def save(self, device: Device) -> Device:
        """Validate a device, then insert or update it.

        Args:
            device: Device to save; inserted when ``id`` is None.

        Returns:
            The same device with ``id`` set.

        Raises:
            ValueError: If the device is invalid.
        """
        self.validate(device)
        if device.id is None:
            self.repository.add(device)
            log.info("device added", extra={"context": {"id": device.id, "name": device.name}})
        else:
            self.repository.update(device)
            log.info("device updated", extra={"context": {"id": device.id, "name": device.name}})
        return device

    def delete(self, device_id: int) -> None:
        """Delete a device.

        Args:
            device_id: Device id.
        """
        self.repository.delete(device_id)
        log.info("device deleted", extra={"context": {"id": device_id}})
