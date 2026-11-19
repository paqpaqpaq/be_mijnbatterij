"""Sensoren: resultaat van vandaag, dekking en status."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity, SensorEntityDescription,
                                             SensorStateClass)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSIE
from .coordinator import BEMBCoordinator


@dataclass(frozen=True, kw_only=True)
class BEMBSensorBeschrijving(SensorEntityDescription):
    waarde: Callable[[dict[str, Any]], Any]
    extra: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _rond(n: Any, d: int) -> Any:
    return round(n, d) + 0.0 if isinstance(n, (int, float)) else None  # + 0.0: geen -0.0


SENSOREN = (
    BEMBSensorBeschrijving(key="totaal", translation_key="totaal", device_class=SensorDeviceClass.MONETARY,
                           native_unit_of_measurement="EUR", suggested_display_precision=2, waarde=lambda d: _rond(d["totaal"], 4)),
    BEMBSensorBeschrijving(key="handel", translation_key="handel", device_class=SensorDeviceClass.MONETARY,
                           native_unit_of_measurement="EUR", suggested_display_precision=2, waarde=lambda d: _rond(d["handel"], 4)),
    BEMBSensorBeschrijving(key="besparing", translation_key="besparing", device_class=SensorDeviceClass.MONETARY,
                           native_unit_of_measurement="EUR", suggested_display_precision=2, waarde=lambda d: _rond(d["besparing"], 4)),
    BEMBSensorBeschrijving(key="geladen", translation_key="geladen", device_class=SensorDeviceClass.ENERGY,
                           state_class=SensorStateClass.TOTAL_INCREASING, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                           suggested_display_precision=2, waarde=lambda d: _rond(d["geladen"], 3)),
    BEMBSensorBeschrijving(key="ontladen", translation_key="ontladen", device_class=SensorDeviceClass.ENERGY,
                           state_class=SensorStateClass.TOTAL_INCREASING, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                           suggested_display_precision=2, waarde=lambda d: _rond(d["ontladen"], 3)),
    BEMBSensorBeschrijving(key="dekking", translation_key="dekking", native_unit_of_measurement=PERCENTAGE,
                           entity_category=EntityCategory.DIAGNOSTIC, suggested_display_precision=1,
                           waarde=lambda d: _rond(100 * d["dekking"], 2) if d["dekking"] is not None else None,
                           extra=lambda d: {"kwartieren_zonder_prijs": d["zonder_prijs"], "kwartieren": d["kwartieren"]}),
    BEMBSensorBeschrijving(key="soc", translation_key="soc", device_class=SensorDeviceClass.BATTERY,
                           state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE, waarde=lambda d: d["soc"]),
    BEMBSensorBeschrijving(key="laatste_live", translation_key="laatste_live", device_class=SensorDeviceClass.TIMESTAMP,
                           entity_category=EntityCategory.DIAGNOSTIC,
                           waarde=lambda d: datetime.fromtimestamp(d["laatste_live"], timezone.utc) if d["laatste_live"] else None),
    BEMBSensorBeschrijving(key="status", translation_key="status", entity_category=EntityCategory.DIAGNOSTIC,
                           waarde=lambda d: d["status"][:250], extra=lambda d: {"activiteit": d["log"], "dagen": d["dagen"], "omvormerrendement": d.get("rendement")}),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c: BEMBCoordinator = entry.runtime_data
    async_add_entities(BEMBSensor(c, b) for b in SENSOREN)


class BEMBSensor(CoordinatorEntity[BEMBCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: BEMBSensorBeschrijving

    def __init__(self, coordinator: BEMBCoordinator, beschrijving: BEMBSensorBeschrijving) -> None:
        super().__init__(coordinator)
        self.entity_description = beschrijving
        uid = coordinator.entry.unique_id or coordinator.entry.entry_id
        self._attr_unique_id = f"{uid}_{beschrijving.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uid)}, name="BE Mijnbatterij", manufacturer="paQ",
                                            model="Balans → Mijnbatterij", sw_version=VERSIE, entry_type=DeviceEntryType.SERVICE)

    @property
    def native_value(self) -> Any:
        return self.entity_description.waarde(self.coordinator.data or {}) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.extra or not self.coordinator.data:
            return None
        return self.entity_description.extra(self.coordinator.data)
