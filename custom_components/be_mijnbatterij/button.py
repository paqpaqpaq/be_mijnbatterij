"""Knop: nu live en alle gewijzigde dagen versturen."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BEError, MBError
from .const import DOMAIN, VERSIE
from .coordinator import BEMBCoordinator


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([NuVersturen(entry.runtime_data)])


class NuVersturen(CoordinatorEntity[BEMBCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "nu_versturen"

    def __init__(self, coordinator: BEMBCoordinator) -> None:
        super().__init__(coordinator)
        uid = coordinator.entry.unique_id or coordinator.entry.entry_id
        self._attr_unique_id = f"{uid}_nu_versturen"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uid)}, name="BE Mijnbatterij", manufacturer="paQ",
                                            model="Balans → Mijnbatterij", sw_version=VERSIE, entry_type=DeviceEntryType.SERVICE)

    async def async_press(self) -> None:
        e = self.coordinator.engine
        try:
            await e.verzamel()
            try:
                await e.live()
            except MBError as err:
                e.noteer(f"Live: {err}")
            await e.dag_ronde(True)
            if not e.proef:
                await e.heartbeat()
        except (BEError, MBError) as err:
            e.noteer(str(err))
        self.coordinator.bewaar()
        self.coordinator.async_set_updated_data(self.coordinator.samenvatting())
