"""Schakelaars op het apparaat: dezelfde instellingen als in Configureren, maar zichtbaar."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OPT_AFRR, OPT_PROEF, OPT_SALDEERT, OPT_VERSTUREN, STANDAARD_OPTIES, VERSIE
from .coordinator import BEMBCoordinator

SCHAKELAARS = [
    (OPT_VERSTUREN, "mdi:cloud-upload", None),
    (OPT_PROEF, "mdi:test-tube", None),
    (OPT_SALDEERT, "mdi:swap-horizontal", EntityCategory.CONFIG),
    (OPT_AFRR, "mdi:sine-wave", EntityCategory.CONFIG),
]


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([Optie(entry.runtime_data, k, icoon, cat) for k, icoon, cat in SCHAKELAARS])


class Optie(CoordinatorEntity[BEMBCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BEMBCoordinator, sleutel: str, icoon: str, categorie: EntityCategory | None) -> None:
        super().__init__(coordinator)
        uid = coordinator.entry.unique_id or coordinator.entry.entry_id
        self._sleutel = sleutel
        self._attr_translation_key = sleutel
        self._attr_unique_id = f"{uid}_optie_{sleutel}"
        self._attr_icon = icoon
        self._attr_entity_category = categorie
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uid)}, name="BE Mijnbatterij", manufacturer="paQ",
                                            model="Balans → Mijnbatterij", sw_version=VERSIE, entry_type=DeviceEntryType.SERVICE)

    @property
    def is_on(self) -> bool:
        return bool({**STANDAARD_OPTIES, **self.coordinator.entry.options}.get(self._sleutel))

    async def _zet(self, waarde: bool) -> None:
        entry = self.coordinator.entry
        # Wijziging loopt via de opties: de update-listener geeft ze door aan de engine.
        self.hass.config_entries.async_update_entry(entry, options={**STANDAARD_OPTIES, **entry.options, self._sleutel: waarde})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._zet(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._zet(False)
