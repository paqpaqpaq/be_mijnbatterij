"""Verzamelen, berekenen en versturen — los van Home Assistant, zodat het testbaar is.

Afstemming met de browserplugin (beide schrijven naar hetzelfde account):
- Beide rekenen met calc.py-regels (plugin = JS-kopie).
- Elk dagrecord krijgt een label [BEMB ... bron=ha|browser type=be|cache dekking=..].
- Home Assistant zet elke 15 min 'actief=<tijd>' in één dagrecord (heartbeat).
  De plugin ziet dat en stopt dan met live en dagen; valt HA weg, dan neemt
  de plugin het na ±40 min vanzelf weer over.
- Definitief (BE) wint altijd van voorlopig; voorlopig vervangt voorlopig
  alleen bij minstens gelijke dekking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import calc
from .api import TenergyClient, BEClient, BEError, BERateLimited, MBClient, MBError

_LOGGER = logging.getLogger(__name__)

KW_DAGEN = 10
PRIJS_DAGEN = 16
DEF_DAGEN = 40
DAG_VENSTER = 10
HEARTBEAT_S = 15 * 60
BROWSER_ACTIEF_MS = 25 * 60_000
HISTORIE_PAUZE_S = 4.0  # ~15 BE-verzoeken per minuut extra tijdens historie  # browserhartslag (elke 10 min) geldt zo lang
DAGRONDE_S = 30 * 60
MAAND_TTL_S = 2 * 3600


def _nu_ms() -> int:
    return int(time.time() * 1000)


class Engine:
    def __init__(self, be: BEClient, mb: MBClient, state: dict, opties: dict, versie: str,
                 now_ms: Callable[[], int] = _nu_ms, slaap: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 tenergy: TenergyClient | None = None) -> None:
        self.be, self.mb, self.versie = be, mb, versie
        self.tenergy = tenergy
        self.opties = opties
        self.now_ms = now_ms
        self.slaap = slaap
        self.s = state
        for key, leeg in (("kw", {}), ("prijzen", {}), ("def", {}), ("sent", {}), ("maand", {}), ("hersteld", {}),
                          ("tenergy", {}), ("tenergy_act", {})):
            self.s.setdefault(key, leeg)
        self.s.setdefault("eta_som", calc.kalibratie_leeg())
        self.log: list[str] = []
        self.laatste_live: dict | None = None
        self.laatste_dagronde = 0.0
        self.laatste_heartbeat = float(self.s.get("heartbeat_at", 0))
        self._maand_cache: dict[str, tuple[float, dict]] = {}
        self._prijs_tik = 0
        self._lock = asyncio.Lock()
        # Pauze vóór elk BE-verzoek voor een dag/maand; tijdens historie ruimer (verzoekbudget BE).
        self.be_pauze = 1.25
        self.historie_bezig = False
        self.vandaag: dict | None = None
        self._tenergy_at = 0.0
        self.tenergy_fout = ""
        # Heartbeat kon niet worden geplaatst: de browserplugin heeft betere dagen en verstuurt
        # dan ook live; Home Assistant stuurt dan geen live om elkaar niet te overschrijven.
        self.hb_mislukt = False
        self.hb_poging = 0.0
        self.browser_hartslag = 0  # ms; laatste actief= van de browserplugin

    # ── hulpjes ──────────────────────────────────────────────────────────
    def noteer(self, tekst: str) -> None:
        stempel = time.strftime("%H:%M", time.localtime(self.now_ms() / 1000))
        self.log.insert(0, f"{stempel}  {tekst}")
        del self.log[60:]
        _LOGGER.debug(tekst)

    @property
    def browser_verstuurt(self) -> bool:
        """Kon HA zijn heartbeat niet kwijt en geeft de plugin hartslag (≤ 25 min), dan verstuurt die."""
        return (self.hb_mislukt and not self.proef
                and self.now_ms() - self.browser_hartslag <= BROWSER_ACTIEF_MS)

    @property
    def saldeert(self) -> bool:
        return bool(self.opties.get("saldeert", True))

    @property
    def afrr(self) -> bool:
        return bool(self.opties.get("afrr", True))

    @property
    def proef(self) -> bool:
        return bool(self.opties.get("proef", False))

    @property
    def versturen(self) -> bool:
        return bool(self.opties.get("versturen", True))

    @property
    def eta(self) -> dict:
        return calc.kalibratie_eta(self.s["eta_som"])

    def kw_dag(self, dk: str) -> dict[int, dict]:
        return {int(q): dict(v) for q, v in (self.s["kw"].get(dk) or {}).items()}

    def prijs_index(self) -> dict[int, dict]:
        return {int(q): v for q, v in self.s["prijzen"].items()}

    async def installatie(self) -> str:
        if not self.s.get("installatie"):
            self.s["installatie"] = await self.be.zoek_installatie()
        self.be.installatie = self.s["installatie"]
        return self.s["installatie"]

    # ── verzamelen ───────────────────────────────────────────────────────
    async def verzamel(self) -> None:
        """Ruwe metingen incrementeel naar kwartieren; prijzen naar het archief."""
        inst = await self.installatie()
        nu = self.now_ms()
        laatste = self.s.get("laatste")
        van = max(nu - 26 * 3600_000, int(laatste["t"]) if laatste else 0)
        j = await self.be.current_history(inst, van, nu)
        res = j.get("resolutionMs") if calc.getal((j or {}).get("resolutionMs")) else 0
        rows = sorted((r for r in (j or {}).get("rows") or []
                       if isinstance(r, dict) and calc.getal(r.get("t")) and calc.getal(r.get("po"))
                       and (not laatste or r["t"] > laatste["t"]) and r["t"] <= nu + 60_000), key=lambda r: r["t"])
        nieuw: dict[int, dict] = {}
        vorige = laatste
        for r in rows:
            n = {"t": int(r["t"]), "po": r["po"], "r": res}
            if calc.getal(r.get("bo")):
                n["bo"] = r["bo"]
            if calc.getal(r.get("s")):
                n["s"] = r["s"]
            calc.kalibratie_tel(self.s["eta_som"], r)
            if vorige:
                # Bestaande kwartierwaarden worden aangevuld, niet vervangen.
                for q in range((int(vorige["t"]) // calc.Q) * calc.Q, n["t"] + 1, calc.Q):
                    if q not in nieuw:
                        oud = (self.s["kw"].get(calc.dag_van(q)) or {}).get(str(q))
                        if oud:
                            nieuw[q] = dict(oud)
                calc.integreer_segment(nieuw, vorige, n, self.eta)
            vorige = n
        for q, c in nieuw.items():
            self.s["kw"].setdefault(calc.dag_van(q), {})[str(q)] = c
        if vorige:
            self.s["laatste"] = vorige
        # Kalibratie laten meebewegen: oude metingen wegen langzaam minder.
        som = self.s["eta_som"]
        if sum(c[2] for naam in ("uit", "in") for c in som[naam]) > 40_000:
            for naam in ("uit", "in"):
                som[naam] = [[c[0] / 2, c[1] / 2, c[2] // 2] for c in som[naam]]
        grens = calc.plus_dagen(calc.vandaag(nu), -KW_DAGEN)
        for dk in [d for d in self.s["kw"] if d < grens]:
            del self.s["kw"][dk]

        await self._tenergy(nu)
        self._prijs_tik += 1
        if self._prijs_tik % 2 == 1:
            try:
                arch = self.prijs_index()
                calc.verwerk_prijzen(arch, await self.be.current_prices(), nu, self.s["tenergy"])
                calc.tenergy_aanvullen(arch, self.s["tenergy"], nu, nu - 2 * 86_400_000)
                self._bewaar_prijzen(arch)
            except BERateLimited:
                raise
            except BEError as err:
                self.noteer(f"Prijzen: {err}")
            for dk in (calc.plus_dagen(calc.vandaag(nu), -1), calc.vandaag(nu)):
                await self.herstel_prijzen(dk, 3600)
        pgrens = nu - PRIJS_DAGEN * 86_400_000
        for q in [q for q in self.s["prijzen"] if int(q) < pgrens]:
            del self.s["prijzen"][q]
        self.vandaag = self.dag_uit_metingen(calc.vandaag(nu), nu)

    async def _tenergy(self, nu: int) -> None:
        """Onbalansprijzen van Tenergy ophalen (elke minuut) en ontbrekende kwartieren aanvullen."""
        if not self.tenergy or time.time() - self._tenergy_at < 55:
            return
        self._tenergy_at = time.time()
        try:
            n = calc.tenergy_verwerk(await self.tenergy.pagina(), nu, self.s["tenergy"], self.s["tenergy_act"])
        except (BEError, ValueError) as err:
            if str(err) != self.tenergy_fout:
                self.noteer(f"Onbalansprijzen (Tenergy): {err}")
            self.tenergy_fout = str(err)
            return
        if self.tenergy_fout:
            self.noteer(f"Onbalansprijzen (Tenergy): weer bereikbaar ({n} kwartieren)")
        self.tenergy_fout = ""
        arch = self.prijs_index()
        if calc.tenergy_aanvullen(arch, self.s["tenergy"], nu, nu - 2 * 86_400_000):
            self._bewaar_prijzen(arch)

    def _bewaar_prijzen(self, arch: dict[int, dict]) -> None:
        self.s["prijzen"] = {str(k): v for k, v in arch.items()}

    async def herstel_prijzen(self, dk: str, pauze_s: int) -> None:
        start, eind = calc.grenzen(dk)
        nu = self.now_ms()
        tot = min(eind, (nu // calc.Q) * calc.Q)
        if tot <= start:
            return
        arch = self.prijs_index()
        voor = calc.prijs_gaten(arch, start, tot)
        if not voor or nu / 1000 - float(self.s["hersteld"].get(dk, 0)) < pauze_s:
            return
        try:
            j = await self.be.current_prices(start, tot)
        except BERateLimited:
            raise
        except BEError as err:
            if "400" in str(err):
                self.s["hersteld"][dk] = nu / 1000 + 20 * 3600
            self.noteer(f"Prijzen {dk}: {err}")
            return
        arch = self.prijs_index()
        n = calc.verwerk_prijzen(arch, j, nu, self.s["tenergy"])
        self._bewaar_prijzen(arch)
        self.s["hersteld"][dk] = nu / 1000
        na = calc.prijs_gaten(self.prijs_index(), start, tot)
        self.noteer(f"Prijzen {dk}: {voor - na} kwartieren hersteld" + (f", {na} nog zonder prijs ({n} rijen van BE)" if na else ""))

    # ── berekenen ────────────────────────────────────────────────────────
    def dag_uit_metingen(self, dk: str, tot: int | None = None) -> dict:
        start, eind = calc.grenzen(dk)
        einde = min(eind, tot or eind)
        duur = max(1.0, (einde - start) / 1000)
        kw = {q: x for q, x in calc.zet_prijzen(self.kw_dag(dk), self.prijs_index()).items() if q < einde}
        w = calc.waardeer(kw, self.saldeert, None)
        return {"dk": dk, "bron": "cache", "kw": kw, "w": w, "eta": self.eta,
                "dekking": min(1.0, w["accuSec"] / duur), "prijsDekking": min(1.0, w["prijsSec"] / duur),
                "kwal": min(1.0, w["vastSec"] / duur)}

    async def maand_bron(self, ym: str, ttl_s: float) -> dict:
        m = self._maand_cache.get(ym)
        if m and time.time() - m[0] < ttl_s:
            return m[1]
        j = await self.be.results(await self.installatie(), "month", ym)
        if not j or j.get("interval") != "month" or j.get("period") != ym or not isinstance(j.get("series"), list):
            raise BEError(f"Onverwacht maandantwoord voor {ym}")
        self._maand_cache[ym] = (time.time(), j)
        return j

    async def haal_definitief(self, dk: str, rij: dict, force: bool) -> dict[int, dict]:
        import json as _json
        sig = _json.dumps(rij, sort_keys=True)
        st = self.s["def"].get(dk)
        nu = self.now_ms()
        recent = nu - calc.grenzen(dk)[1] < 7 * 86_400_000
        if st and st.get("v") == 2 and st.get("sig") == sig and (st.get("kw") or (st.get("fout") and not force)):
            houdbaar = 86_400 if st.get("fout") else (6 * 3600 if recent else 30 * 86_400)
            if nu / 1000 - st["at"] < houdbaar:
                if st.get("fout"):
                    raise calc.BronFout(st["fout"] + " (eerdere controle)")
                return {int(q): v for q, v in st["kw"].items()}
        await self.slaap(self.be_pauze)
        j = await self.be.results(await self.installatie(), "day", dk)
        try:
            kw = calc.parse_definitief(j, rij, dk)
        except calc.BronFout as err:
            self.s["def"][dk] = {"v": 2, "sig": sig, "at": nu / 1000, "fout": str(err)}
            raise
        self.s["def"][dk] = {"v": 2, "sig": sig, "at": nu / 1000, "kw": {str(q): v for q, v in kw.items()}}
        return kw

    async def dag_definitief(self, dk: str, rij: dict, force: bool) -> dict:
        bron = await self.haal_definitief(dk, rij, force)
        d = calc.verdeel_definitief(bron, self.kw_dag(dk), self.eta)
        if d["ontbrekend"] > 4:
            raise calc.BronFout(f"{d['ontbrekend']} kwartieren zonder accuwaarden, ook niet in eigen metingen")
        kw = calc.kies_prijzen(d["kw"], bron, self.prijs_index(), rij)
        w = calc.waardeer(kw, self.saldeert, calc.afrr_van(rij) if self.afrr else None)
        start, eind = calc.grenzen(dk)
        duur = (eind - start) / 1000
        return {"dk": dk, "bron": "BE", "kw": kw, "w": w, "eta": self.eta, "dekking": min(1.0, w["accuSec"] / duur),
                "prijsDekking": min(1.0, w["prijsSec"] / duur), "aangevuld": d["aangevuld"],
                "kwal": calc.kwaliteit(d, w)}

    async def bepaal_dag(self, dk: str, maand_j: dict | None, force: bool, rapport: list[str]) -> dict | None:
        rij = next((r for r in (maand_j or {}).get("series", []) if isinstance(r, dict) and r.get("date") == dk), None)
        if rij and rij.get("status") == "complete":
            try:
                return await self.dag_definitief(dk, rij, force)
            except BERateLimited:
                raise
            except (calc.BronFout, BEError) as err:
                rapport.append(f"{dk}: BE complete maar niet bruikbaar ({err}); metingen gebruikt.")
        r = self.dag_uit_metingen(dk)
        return r if r["dekking"] >= calc.MIN_DEKKING_VOORLOPIG else None

    # ── versturen ────────────────────────────────────────────────────────
    async def live(self) -> dict | None:
        m = self.s.get("laatste")
        nu = self.now_ms()
        if self.browser_verstuurt:
            raise MBError("de browserplugin verstuurt (hartslag " + time.strftime("%H:%M", time.localtime(self.browser_hartslag / 1000)) + ")")
        if not m or not calc.getal(m.get("bo")) or nu - m["t"] > 300_000:
            raise MBError("Geen actuele accumeting (jonger dan 5 min); live niet verstuurd")
        r = self.dag_uit_metingen(calc.dag_van(m["t"]), int(m["t"]))
        w = r["w"]
        p = {"timestamp": calc.iso_ams(int(m["t"])), "batteryResult": calc.e2(w["totaal"]),
             "batterySavings": calc.e2(w["besparing"]), "chargedToday": calc.k3(w["geladen"]),
             "dischargedToday": calc.k3(w["ontladen"]), "gridImportToday": calc.k3(w["imp"]),
             "gridExportToday": calc.k3(w["exp"]), "invalid": r["dekking"] < 0.99 or w["zonderPrijs"] > 0}
        if calc.getal(m.get("s")) and 0 <= m["s"] <= 100:
            p["batteryCharge"] = calc.to_fixed(m["s"], 1)
        if self.proef:
            p["test"] = True
        await self.mb.live(p)
        self.laatste_live = {"at": nu, "p": p}
        return p

    async def _verstuur_dag(self, r: dict, bestaand: dict | None, rapport: list[str], actief: str | None = None) -> bool:
        type_ = "be" if r["bron"] == "BE" else "cache"
        mag, reden = calc.mag_overschrijven(bestaand, type_, r["dekking"], r.get("kwal"))
        if not mag:
            rapport.append(f"{r['dk']}: niet vervangen — {reden}.")
            return False
        p = calc.dag_payload(r, self.saldeert, self.afrr, self.versie, "ha", actief)
        h = calc.payload_hash(p)
        sleutel = "proef:" + r["dk"] if self.proef else r["dk"]
        st = self.s["sent"].get(sleutel)
        if not actief and st and st.get("hash") == h:
            return False
        body = dict(p, test=True) if self.proef else p
        await self.mb.dag(body)
        self.s["sent"][sleutel] = {"hash": h, "bron": r["bron"], "at": self.now_ms(), "totaal": p["batteryResult"],
                                   "besparing": p["batterySavings"]}
        if not actief:
            rapport.append(f"{r['dk']}: {'gevalideerd (proef)' if self.proef else 'verstuurd'} · {r['bron']} · € {p['batteryResult']}"
                           f" (besparing € {p['batterySavings']})" + (" · onvolledig" if p["invalid"] else ""))
        return True

    async def dag_ronde(self, force: bool = False) -> list[str]:
        async with self._lock:
            nu = self.now_ms()
            vk = calc.vandaag(nu)
            dagen = [calc.plus_dagen(vk, -i) for i in range(DAG_VENSTER, 0, -1)]
            rapport: list[str] = []
            maand_j: dict[str, dict] = {}
            for ym in sorted({d[:7] for d in dagen}):
                try:
                    maand_j[ym] = await self.maand_bron(ym, 60 if force else MAAND_TTL_S)
                except BERateLimited as err:
                    rapport.append(str(err))
                    break
                except BEError as err:
                    rapport.append(f"{ym}: {err}")
            try:
                bestaand = await self.mb.dagen(dagen[0], dagen[-1])
            except MBError as err:
                rapport.append(f"Mijnbatterij: {err}")
                return rapport
            for dk in dagen:
                try:
                    r = await self.bepaal_dag(dk, maand_j.get(dk[:7]), force, rapport)
                    if r:
                        await self._verstuur_dag(r, bestaand.get(dk), rapport)
                except BERateLimited as err:
                    rapport.append(str(err))
                    break
                except (MBError, BEError) as err:
                    rapport.append(f"{dk}: {err}")
            self.laatste_dagronde = time.time()
            if not rapport:
                rapport.append("Dagen: niets gewijzigd.")
            for regel in rapport:
                self.noteer(regel)
            return rapport

    async def heartbeat(self) -> str | None:
        """Laat de plugin weten dat Home Assistant verstuurt (zie moduledocstring)."""
        if self.proef:
            return None
        async with self._lock:
            nu = self.now_ms()
            vk = calc.vandaag(nu)
            dagen = [calc.plus_dagen(vk, -i) for i in range(1, DAG_VENSTER + 1)]
            bestaand = await self.mb.dagen(dagen[-1], dagen[0])
            for rec in bestaand.values():
                lab = calc.lees_label((rec or {}).get("note"))
                if lab and lab.get("bron") == "browser" and lab.get("actief"):
                    try:
                        t = int(datetime.fromisoformat(lab["actief"]).timestamp() * 1000)
                    except ValueError:
                        continue
                    self.browser_hartslag = max(self.browser_hartslag, t)
            for dk in dagen:
                try:
                    maand_j = await self.maand_bron(dk[:7], MAAND_TTL_S)
                except BEError:
                    maand_j = None
                r = await self.bepaal_dag(dk, maand_j, False, [])
                if not r:
                    continue
                mag, _ = calc.mag_overschrijven(bestaand.get(dk), "be" if r["bron"] == "BE" else "cache", r["dekking"], r.get("kwal"))
                if not mag:
                    continue
                await self._verstuur_dag(r, bestaand.get(dk), [], actief=calc.iso_ams(nu))
                self.hb_mislukt = False
                self.laatste_heartbeat = time.time()
                self.s["heartbeat_at"] = self.laatste_heartbeat
                return dk
            if not self.hb_mislukt:
                self.noteer("Heartbeat: de browserplugin heeft alle recente dagen nauwkeuriger; zolang die hartslag geeft, laat HA live aan de plugin.")
            self.hb_mislukt = True
            self.hb_poging = time.time()
            return None

    async def historie(self, van_ym: str, stop: Callable[[], bool] = lambda: False) -> list[str]:
        """Alle BE-complete dagen vanaf een maand. Draait in de achtergrond: per dag even het slot,
        zodat live en de dagronde gewoon doorlopen; elk BE-verzoek met HISTORIE_PAUZE_S ertussen."""
        if self.historie_bezig:
            raise BEError("historie loopt al")
        self.historie_bezig, oude_pauze = True, self.be_pauze
        self.be_pauze = HISTORIE_PAUZE_S
        try:
            return await self._historie(van_ym, stop)
        finally:
            self.be_pauze, self.historie_bezig = oude_pauze, False

    async def _historie(self, van_ym: str, stop: Callable[[], bool]) -> list[str]:
        nu = self.now_ms()
        vk = calc.vandaag(nu)
        rapport: list[str] = []
        for ym in calc.maanden(van_ym, vk[:7]):
            if stop():
                rapport.append("Gestopt.")
                break
            self.noteer(f"Historie: {ym} …")
            try:
                await self.slaap(self.be_pauze)
                j = await self.maand_bron(ym, 60)
            except BERateLimited as err:
                rapport.append(str(err))
                break
            except BEError as err:
                rapport.append(f"{ym}: {err}")
                continue
            dagen = sorted(r["date"] for r in j["series"] if isinstance(r, dict) and isinstance(r.get("date"), str)
                           and r["date"][:7] == ym and r["date"] < vk)
            try:
                bestaand = await self.mb.dagen(dagen[0], dagen[-1]) if dagen else {}
            except MBError as err:
                rapport.append(f"{ym}: Mijnbatterij {err}")
                continue
            som = {"t": 0.0, "b": 0.0, "l": 0.0, "o": 0.0, "n": 0}
            alles = True
            for dk in dagen:
                if stop():
                    break
                rij = next((r for r in j["series"] if isinstance(r, dict) and r.get("date") == dk), None)
                if not rij or rij.get("status") != "complete":
                    alles = False
                    continue
                try:
                    async with self._lock:
                        r = await self.dag_definitief(dk, rij, False)
                        await self._verstuur_dag(r, bestaand.get(dk), rapport)
                    w = r["w"]
                    som["t"] += w["totaal"]; som["b"] += w["besparing"]
                    som["l"] += w["geladen"]; som["o"] += w["ontladen"]; som["n"] += 1
                except BERateLimited as err:
                    rapport.append(str(err))
                    return rapport
                except (calc.BronFout, BEError, MBError) as err:
                    alles = False
                    rapport.append(f"{dk}: {err}")
            if ym < vk[:7] and alles and som["n"] and som["n"] == len(dagen):
                mp = {"yearMonth": ym, "batteryResult": calc.e2(som["t"]), "batterySavings": calc.e2(som["b"]),
                      "batteryCharged": calc.k3(som["l"]), "batteryDischarged": calc.k3(som["o"]),
                      "partial": som["n"] < _dagen_in_maand(ym),
                      "note": f"Som van {som['n']} BE-definitieve dagen. " + calc.maak_label(self.versie, "ha", "be", 1.0)}
                import json as _json
                h = _json.dumps(mp, sort_keys=True)
                if self.proef:
                    rapport.append(f"{ym}: maandtotaal € {mp['batteryResult']} — proefstand, niet verstuurd.")
                elif self.s["maand"].get(ym) != h:
                    try:
                        await self.mb.maand(mp)
                        self.s["maand"][ym] = h
                        rapport.append(f"{ym}: maandtotaal verstuurd € {mp['batteryResult']}.")
                    except MBError as err:
                        rapport.append(f"{ym}: maandtotaal mislukt: {err}")
            elif ym < vk[:7]:
                rapport.append(f"{ym}: {som['n']}/{len(dagen)} dagen definitief; maandtotaal niet vervangen.")
            self.noteer(f"Historie {ym}: {som['n']}/{len(dagen)} dagen, € {calc.e2(som['t'])}")
        return rapport

    def opruimen(self) -> None:
        grens = calc.plus_dagen(calc.vandaag(self.now_ms()), -DEF_DAGEN)
        for key in ("def", "sent"):
            for dk in [d for d in self.s[key] if d.replace("proef:", "") < grens]:
                del self.s[key][dk]


def _dagen_in_maand(ym: str) -> int:
    import calendar
    return calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]
