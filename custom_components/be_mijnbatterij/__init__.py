"""BE Mijnbatterij: Balans Energie-resultaten naar mijnbatterij.nl, gesplitst in handel en besparing."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .api import BEClient, MBClient
from .const import CONF_BE_PASS, CONF_BE_USER, CONF_MB_KEY, DOMAIN, VERSIE
from .coordinator import BEMBCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
SCHEMA_HISTORIE = vol.Schema({vol.Required("vanaf"): vol.Match(r"^\d{4}-(0[1-9]|1[0-2])$")})

type BEMBConfigEntry = ConfigEntry[BEMBCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BEMBConfigEntry) -> bool:
    # Eigen cookie-jar: de BE-sessie blijft gescheiden van de rest van Home Assistant.
    be_sessie = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    be = BEClient(be_sessie, entry.data[CONF_BE_USER], entry.data[CONF_BE_PASS])
    mb = MBClient(async_get_clientsession(hass), entry.data[CONF_MB_KEY], VERSIE)
    coordinator = BEMBCoordinator(hass, entry, be, mb)
    await coordinator.async_laad()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_opties_gewijzigd))

    if not hass.services.has_service(DOMAIN, "historie_herstellen"):
        async def historie(call: ServiceCall) -> dict:
            uit = {}
            for e in _geladen(hass):
                rapport = await e.runtime_data.engine.historie(call.data["vanaf"])
                e.runtime_data.bewaar()
                uit[e.title] = rapport
            return {"rapport": uit}

        hass.services.async_register(DOMAIN, "historie_herstellen", historie, schema=SCHEMA_HISTORIE,
                                     supports_response=SupportsResponse.OPTIONAL)
    return True


def _geladen(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


async def _opties_gewijzigd(hass: HomeAssistant, entry: BEMBConfigEntry) -> None:
    entry.runtime_data.engine.opties = entry.runtime_data.opties()
    await entry.runtime_data.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: BEMBConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.store.async_save(entry.runtime_data.engine.s)
        if not [e for e in _geladen(hass) if e.entry_id != entry.entry_id]:
            hass.services.async_remove(DOMAIN, "historie_herstellen")
    return ok
