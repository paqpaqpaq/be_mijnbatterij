"""Config flow: BE-inlog en Mijnbatterij-sleutel, plus opties."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .api import BEAuthError, BEClient, BEError, MBAuthError, MBClient, MBError
from .const import (CONF_BE_PASS, CONF_BE_USER, CONF_MB_KEY, DOMAIN, OPT_AFRR, OPT_PROEF, OPT_SALDEERT,
                    OPT_VERSTUREN, STANDAARD_OPTIES, VERSIE)

SCHEMA = vol.Schema({
    vol.Required(CONF_BE_USER): str,
    vol.Required(CONF_BE_PASS): str,
    vol.Required(CONF_MB_KEY): str,
})


async def _controleer(hass, data: Mapping[str, Any]) -> tuple[str, str]:
    """Inloggen bij BE, installatie zoeken en de Mijnbatterij-sleutel testen."""
    be = BEClient(async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar()), data[CONF_BE_USER], data[CONF_BE_PASS])
    await be.login()
    installatie = await be.zoek_installatie()
    me = await MBClient(async_get_clientsession(hass), data[CONF_MB_KEY], VERSIE).me()
    return installatie, str((me or {}).get("username") or (me or {}).get("name") or "")


SCHEMA_NIEUW = SCHEMA.extend({vol.Required(OPT_PROEF, default=False): bool})


class BEMBConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        fouten: dict[str, str] = {}
        if user_input is not None:
            try:
                installatie, naam = await _controleer(self.hass, user_input)
            except BEAuthError:
                fouten["base"] = "be_auth"
            except MBAuthError:
                fouten["base"] = "mb_auth"
            except (BEError, MBError, aiohttp.ClientError, TimeoutError):
                fouten["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(installatie)
                self._abort_if_unique_id_configured()
                data = {k: v for k, v in user_input.items() if k != OPT_PROEF}
                return self.async_create_entry(title=f"Balans {installatie}" + (f" · {naam}" if naam else ""),
                                               data=data, options={**STANDAARD_OPTIES, OPT_PROEF: bool(user_input.get(OPT_PROEF))})
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(SCHEMA_NIEUW, user_input),
                                    errors=fouten)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        fouten: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            try:
                await _controleer(self.hass, data)
            except BEAuthError:
                fouten["base"] = "be_auth"
            except MBAuthError:
                fouten["base"] = "mb_auth"
            except (BEError, MBError, aiohttp.ClientError, TimeoutError):
                fouten["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reauth_confirm", data_schema=self.add_suggested_values_to_schema(
            SCHEMA, {CONF_BE_USER: entry.data[CONF_BE_USER]}), errors=fouten)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BEMBOptionsFlow()


class BEMBOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = {**STANDAARD_OPTIES, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(OPT_VERSTUREN, default=o[OPT_VERSTUREN]): bool,
            vol.Required(OPT_PROEF, default=o[OPT_PROEF]): bool,
            vol.Required(OPT_SALDEERT, default=o[OPT_SALDEERT]): bool,
            vol.Required(OPT_AFRR, default=o[OPT_AFRR]): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
