"""Clients voor het Balans Energie-dashboard en de Mijnbatterij-API."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BE_BASE = "https://dashboard.balansenergie.nl"
MB_BASE = "https://api.mijnbatterij.nl"
LOGIN_PATH = "/accounts/login/"
TIMEOUT = aiohttp.ClientTimeout(total=30)
_CSRF_RE = re.compile(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']|value=["\']([^"\']+)["\']\s+name=["\']csrfmiddlewaretoken["\']')
_KLANT_RE = re.compile(r"/customer/([A-Za-z0-9_-]+)/(?:results|account)(?:/|[\"'?])")
_ONGELDIG = re.compile(r"^(?:account|results|current-history|current-prices|undefined|null)$", re.I)


class BEError(Exception):
    """Algemene BE-fout."""


class BEAuthError(BEError):
    """Inloggen bij BE mislukt (gebruikersnaam/wachtwoord)."""


class BERateLimited(BEError):
    """BE beperkt verzoeken (HTTP 429)."""

    def __init__(self, until: float) -> None:
        super().__init__(f"BE beperkt verzoeken tot {time.strftime('%H:%M', time.localtime(until))}")
        self.until = until


class MBError(Exception):
    """Mijnbatterij-fout."""


class MBAuthError(MBError):
    """API-sleutel ongeldig."""


class BEClient:
    """Sessie op het BE-klantdashboard (Django-login met CSRF)."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._s = session
        self._user = username
        self._pw = password
        self._lock = asyncio.Lock()
        self._ingelogd = False
        self.retry_until = 0.0
        self.installatie: str | None = None

    async def login(self) -> None:
        async with self._s.get(BE_BASE + LOGIN_PATH, timeout=TIMEOUT) as r:
            html = await r.text()
        m = _CSRF_RE.search(html)
        if not m:
            raise BEError("Inlogpagina zonder CSRF-token; BE heeft de login gewijzigd")
        token = m.group(1) or m.group(2)
        data = {"csrfmiddlewaretoken": token, "username": self._user, "password": self._pw}
        headers = {"Referer": BE_BASE + LOGIN_PATH, "Origin": BE_BASE}
        async with self._s.post(BE_BASE + LOGIN_PATH, data=data, headers=headers, allow_redirects=False, timeout=TIMEOUT) as r:
            ok = r.status in (301, 302, 303) and LOGIN_PATH not in (r.headers.get("Location") or "")
            status = r.status
        if not ok:
            self._ingelogd = False
            if status == 200:
                raise BEAuthError("Gebruikersnaam of wachtwoord onjuist")
            raise BEError(f"Inloggen mislukt (HTTP {status})")
        self._ingelogd = True

    async def zoek_installatie(self) -> str:
        """Installatie-id uit de dashboardlinks, net als de plugin."""
        if not self._ingelogd:
            await self.login()
        async with self._s.get(BE_BASE + "/customer/", timeout=TIMEOUT) as r:
            html = await r.text()
            pad = str(r.url.path)
        m = re.match(r"^/customer/([^/]+)(?:/|$)", pad)
        if m and not _ONGELDIG.match(m.group(1)):
            self.installatie = m.group(1)
            return m.group(1)
        for kandidaat in _KLANT_RE.findall(html):
            if not _ONGELDIG.match(kandidaat):
                self.installatie = kandidaat
                return kandidaat
        raise BEError("Geen installatie gevonden op het dashboard")

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if time.time() < self.retry_until:
            raise BERateLimited(self.retry_until)
        async with self._lock:
            if not self._ingelogd:
                await self.login()
            for poging in range(2):
                async with self._s.get(BE_BASE + path, params={k: str(v) for k, v in (params or {}).items()},
                                       headers={"Accept": "application/json"}, allow_redirects=False, timeout=TIMEOUT) as r:
                    if r.status == 429:
                        ra = r.headers.get("Retry-After")
                        try:
                            wacht = float(ra) if ra else 60.0
                        except ValueError:
                            wacht = 300.0
                        self.retry_until = time.time() + max(2.0, wacht)
                        raise BERateLimited(self.retry_until)
                    loc = r.headers.get("Location") or ""
                    ctype = r.headers.get("Content-Type") or ""
                    if (r.status in (301, 302, 303) and "login" in loc) or (r.status == 200 and "json" not in ctype):
                        # Sessie verlopen: één keer opnieuw inloggen.
                        if poging == 0:
                            self._ingelogd = False
                            await self.login()
                            continue
                        raise BEError("BE-sessie kon niet worden hersteld")
                    if r.status != 200:
                        raise BEError(f"BE HTTP {r.status}")
                    return await r.json(content_type=None)
        raise BEError("BE gaf geen antwoord")

    async def current_history(self, installatie: str, start: int, end: int) -> dict:
        return await self.get_json("/customer/current-history/api/", {"installation": installatie, "start": start, "end": end})

    async def current_prices(self, start: int | None = None, end: int | None = None) -> dict:
        params = {"start": start, "end": end} if start is not None and end is not None else None
        return await self.get_json("/customer/current-prices/api/", params)

    async def results(self, installatie: str, interval: str, period: str) -> dict:
        return await self.get_json(f"/customer/{installatie}/results-v2-api/",
                                   {"interval": interval, "period": period, "include_vat": "0", "include_delivery_cost": "0"})


class MBClient:
    """Mijnbatterij-API met Bearer-sleutel. Getallen gaan als strings (spec 1.1.0)."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str, versie: str) -> None:
        self._s = session
        self._key = api_key.strip()
        self._ua = f"BE-Mijnbatterij-HA/{versie}"

    async def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json",
                   "Content-Type": "application/json", "User-Agent": self._ua}
        async with self._s.request(method, MB_BASE + path, data=json.dumps(body) if body is not None else None,
                                   headers=headers, timeout=TIMEOUT) as r:
            text = await r.text()
            if r.status in (401, 403):
                raise MBAuthError(f"Mijnbatterij weigert de sleutel (HTTP {r.status})")
            if not 200 <= r.status < 300:
                raise MBError(f"Mijnbatterij HTTP {r.status}: {text[:300]}")
            try:
                return json.loads(text) if text else {}
            except ValueError:
                return {"text": text}

    async def me(self) -> dict:
        return await self._req("GET", "/api/me")

    async def live(self, body: dict) -> Any:
        return await self._req("POST", "/api/live", body)

    async def dag(self, body: dict) -> Any:
        return await self._req("POST", "/api/results/daily", body)

    async def maand(self, body: dict) -> Any:
        return await self._req("POST", "/api/results/monthly", body)

    async def dagen(self, van: str, tot: str) -> dict[str, dict]:
        j = await self._req("GET", f"/api/results/daily?from={van}&to={tot}")
        lijst = j if isinstance(j, list) else (j or {}).get("results") or []
        return {str(r.get("date", ""))[:10]: r for r in lijst if isinstance(r, dict) and r.get("date")}


class TenergyClient:
    """Publieke Tenergy-pagina met onbalansprijzen per minuut (anoniem)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._s = session

    async def pagina(self) -> str:
        from .calc import TENERGY_URL
        try:
            async with self._s.get(TENERGY_URL, timeout=aiohttp.ClientTimeout(total=25),
                                   headers={"User-Agent": "BE-Mijnbatterij-HA"}) as r:
                if r.status != 200:
                    raise BEError(f"Tenergy HTTP {r.status}")
                return await r.text()
        except BEError:
            raise
        except Exception as err:  # noqa: BLE001 — een externe bron mag de integratie nooit stilleggen
            raise BEError(f"Tenergy niet bereikbaar ({type(err).__name__})") from err


class TennetClient:
    """Openbare TenneT-tabel met settlementprijzen (anoniem, alleen lezen, zonder cookie).

    Eén GET per dag: tabelpagina 90035 met de datum in de URL en alle kwartieren op één pagina."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._s = session

    async def dag(self, dk: str) -> dict[int, list]:
        from . import calc
        datum = f"{dk[8:10]}-{dk[5:7]}-{dk[0:4]}"
        regio = calc.TENNET_REGIO
        t: dict = {}
        try:
            for _ in range(2):
                async with self._s.get(calc.tennet_url(datum, regio), timeout=aiohttp.ClientTimeout(total=25),
                                       headers={"User-Agent": "BE-Mijnbatterij-HA", "Accept": "text/html"}) as r:
                    if r.status != 200:
                        raise BEError(f"TenneT-pagina HTTP {r.status}")
                    html = await r.text()
                t = calc.tennet_tabel(html)
                # Andere regio-id (pagina aangepast): staat in de bladerknop; één keer opnieuw.
                if t.get("regio") and t["regio"] != regio and len(t["rijen"]) < 92:
                    regio = t["regio"]
                    continue
                break
        except (BEError, calc.BronFout):
            raise
        except Exception as err:  # noqa: BLE001 — een externe bron mag de integratie nooit stilleggen
            raise BEError(f"TenneT niet bereikbaar ({type(err).__name__})") from err
        if t.get("leeg"):
            return {}
        # Nooit de prijzen van een andere dag gebruiken: TenneT valt bij een onbekende datum terug op gisteren.
        if t.get("datum") != datum:
            raise BEError(f"TenneT gaf {t.get('datum') or 'geen datum'} in plaats van {datum}")
        return t["rijen"]
