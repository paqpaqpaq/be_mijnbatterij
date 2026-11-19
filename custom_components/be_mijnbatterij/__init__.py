"""BE Mijnbatterij: Balans Energie-resultaten naar mijnbatterij.nl, gesplitst in handel en besparing."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .api import BEClient, MBClient
from .const import CONF_BE_PASS, CONF_BE_USER, CONF_MB_KEY, DOMAIN, OPT_PROEF, OPT_VERSTUREN, VERSIE
from .coordinator import BEMBCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.SWITCH]
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
    _proef_melding(hass, entry)

    if not hass.services.has_service(DOMAIN, "historie_herstellen"):
        async def historie(call: ServiceCall) -> dict:
            """Start de historie in de achtergrond: een actie die minuten duurt, breekt anders af
            zodra de verbinding met de frontend wegvalt ("undefined"). Voortgang staat in de
            activiteit van de status-sensor; aan het eind volgt een melding."""
            gestart = []
            for e in _geladen(hass):
                if e.runtime_data.engine.historie_bezig:
                    raise HomeAssistantError(f"{e.title}: historie loopt al")
                e.async_create_background_task(hass, _historie_taak(hass, e, call.data["vanaf"]), f"{DOMAIN}_historie")
                gestart.append(e.title)
            return {"gestart": gestart, "info": "Voortgang in de activiteit van de status-sensor; aan het eind volgt een melding."}

        hass.services.async_register(DOMAIN, "historie_herstellen", historie, schema=SCHEMA_HISTORIE,
                                     supports_response=SupportsResponse.OPTIONAL)
    return True


async def _historie_taak(hass: HomeAssistant, entry: BEMBConfigEntry, vanaf: str) -> None:
    from homeassistant.components import persistent_notification
    try:
        rapport = await entry.runtime_data.engine.historie(vanaf)
        tekst = "\n".join(f"- {r}" for r in rapport[-40:]) or "- niets gewijzigd"
    except Exception as err:  # noqa: BLE001 — melden in plaats van stil afbreken
        _LOGGER.exception("Historie mislukt")
        tekst = f"Mislukt: {err}"
    entry.runtime_data.bewaar()
    await entry.runtime_data.async_refresh()
    persistent_notification.async_create(hass, tekst, title=f"BE Mijnbatterij — historie vanaf {vanaf}",
                                         notification_id=f"{DOMAIN}_historie_{entry.entry_id}")


def _geladen(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


def _proef_melding(hass: HomeAssistant, entry: BEMBConfigEntry) -> None:
    """Melding onder Instellingen → Reparaties zolang de proefstand aan staat: zo blijft hij niet ongemerkt aan."""
    o = entry.runtime_data.opties()
    issue = f"proefstand_{entry.entry_id}"
    if o.get(OPT_PROEF) and o.get(OPT_VERSTUREN):
        ir.async_create_issue(hass, DOMAIN, issue, is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
                              translation_key="proefstand", translation_placeholders={"naam": entry.title})
    else:
        ir.async_delete_issue(hass, DOMAIN, issue)


async def _opties_gewijzigd(hass: HomeAssistant, entry: BEMBConfigEntry) -> None:
    entry.runtime_data.engine.opties = entry.runtime_data.opties()
    _proef_melding(hass, entry)
    # Schakelaars direct bijwerken, ook als de wijziging uit Configureren komt.
    entry.runtime_data.async_update_listeners()
    await entry.runtime_data.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: BEMBConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        ir.async_delete_issue(hass, DOMAIN, f"proefstand_{entry.entry_id}")
        await entry.runtime_data.store.async_save(entry.runtime_data.engine.s)
        if not [e for e in _geladen(hass) if e.entry_id != entry.entry_id]:
            hass.services.async_remove(DOMAIN, "historie_herstellen")
    return ok
