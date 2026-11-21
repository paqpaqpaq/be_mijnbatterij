"""DataUpdateCoordinator: elke minuut verzamelen, live en dagen versturen."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import calc
from .api import BEAuthError, BEClient, BEError, BERateLimited, MBAuthError, EpexClient, MBClient, MBError, TenergyClient, TennetClient
from .const import DOMAIN, LIVE_S, POLL_S, STANDAARD_OPTIES, VERSIE
from .engine import DAGRONDE_S, HEARTBEAT_S, Engine

_LOGGER = logging.getLogger(__name__)


class BEMBCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, be: BEClient, mb: MBClient) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=POLL_S), config_entry=entry)
        self.entry = entry
        self.store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.engine: Engine | None = None
        self._be, self._mb = be, mb
        self._tenergy = TenergyClient(async_get_clientsession(hass))
        self._tennet = TennetClient(async_get_clientsession(hass))
        self._epex = EpexClient(async_get_clientsession(hass))
        self._laatste_live = 0.0
        self.fout: str = ""

    def opties(self) -> dict:
        return {**STANDAARD_OPTIES, **self.entry.options}

    async def async_laad(self) -> None:
        state = await self.store.async_load() or {}
        self.engine = Engine(self._be, self._mb, state, self.opties(), VERSIE, tenergy=self._tenergy, tennet=self._tennet, epex=self._epex)

    def bewaar(self) -> None:
        self.store.async_delay_save(lambda: self.engine.s if self.engine else {}, 30)

    async def _async_update_data(self) -> dict[str, Any]:
        e = self.engine
        assert e is not None
        e.opties = self.opties()
        try:
            await e.verzamel()
            if e.versturen:
                nu = time.time()
                if nu - self._laatste_live >= LIVE_S - 5:
                    try:
                        await e.live()
                        self._laatste_live = nu
                    except MBError as err:
                        e.noteer(f"Live: {err}")
                if nu - e.laatste_dagronde >= DAGRONDE_S:
                    await e.dag_ronde(False)
                if not e.proef and nu - max(e.laatste_heartbeat, e.hb_poging) >= HEARTBEAT_S:
                    try:
                        await e.heartbeat()
                    except (MBError, BEError) as err:
                        e.noteer(f"Heartbeat: {err}")
                e.opruimen()
            self.fout = ""
        except BEAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MBAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BERateLimited as err:
            self.fout = str(err)
            e.noteer(str(err))
        except (BEError, MBError) as err:
            self.fout = str(err)
            raise UpdateFailed(str(err)) from err
        finally:
            self.bewaar()
        return self.samenvatting()

    def samenvatting(self) -> dict[str, Any]:
        e = self.engine
        v = e.vandaag if e else None
        w = v["w"] if v else {}
        dagen = []
        if e:
            vk = calc.vandaag(e.now_ms())
            for i in range(1, 11):
                dk = calc.plus_dagen(vk, -i)
                st = e.s["sent"].get(("proef:" if e.proef else "") + dk)
                if st:
                    dagen.append({"datum": dk, "bron": st["bron"], "totaal": st["totaal"], "besparing": st["besparing"]})
        top = sorted(((q, x) for q, x in (v or {}).get("kw", {}).items()), key=lambda a: a[0])
        return {
            "totaal": w.get("totaal"), "handel": w.get("handel"), "besparing": w.get("besparing"),
            "geladen": w.get("geladen"), "ontladen": w.get("ontladen"),
            "dekking": (v or {}).get("dekking"), "zonder_prijs": w.get("zonderPrijs"),
            "soc": (e.s.get("laatste") or {}).get("s") if e else None,
            "laatste_live": e.laatste_live["at"] / 1000 if e and e.laatste_live else None,
            "status": self.fout or ("proefstand" if e and e.proef else "browserplugin verstuurt" if e and e.versturen and e.browser_verstuurt
                                   else "actief" if e and e.versturen else "alleen meten"),
            "log": (e.log[:15] if e else []), "dagen": dagen, "kwartieren": len(top),
            "rendement": e.eta if e else None, "rendement_gemeten": e.eta_gemeten if e else None,
        }
