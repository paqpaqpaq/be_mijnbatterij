"""Rekenkern, 1-op-1 gelijk aan de userscript-plugin (BE-Mijnbatterij.user.js).

Alle tijden zijn epoch-milliseconden; energie in kWh, vermogen in W.
Prijzen van BE zijn kaal (excl. btw, energiebelasting en opslag).
Wijzig hier niets zonder de plugin mee te wijzigen: beide schrijven naar
hetzelfde Mijnbatterij-account en moeten exact hetzelfde uitrekenen.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
Q = 900_000
BTW = 1.21
OPSLAG = 0.02
# Energiebelasting excl. btw, schijf 1. Saldering eindigt op 1-1-2027.
EB_EX = {2025: 0.10154, 2026: 0.09161, 2027: 0.08802}
MAX_KAAL = 1.5   # €/kWh: daarboven is een kale kwartierprijs onbruikbaar
MIN_KWH = 0.05   # minimale netstroom voor een bruikbare effectieve prijs
MIN_DEKKING_VOORLOPIG = 0.99  # voorlopige dag alleen bij (vrijwel) volledige metingen
FLOWS = ("ontHuis", "ontNet", "ladNet", "ladPV")


def getal(n: Any) -> bool:
    return isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)


def to_fixed(x: float, n: int) -> str:
    """Zoals JavaScript Number.prototype.toFixed (half naar boven, weg van nul)."""
    d = Decimal(x).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)
    s = f"{d:.{n}f}"
    return "0" + s[2:] if s.startswith("-0") and float(d) == 0 else s


def e2(n: Any) -> str:
    v = n if getal(n) else 0.0
    return to_fixed(0.0 if abs(v) < 0.005 else v, 2)


def k3(n: Any) -> str:
    return to_fixed(max(0.0, n if getal(n) else 0.0), 3)


def k4(n: Any) -> str:
    return to_fixed(max(0.0, n if getal(n) else 0.0), 4)


# ── Tijd ────────────────────────────────────────────────────────────────
def dag_van(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, TZ).date().isoformat()


def vandaag(now_ms: int) -> str:
    return dag_van(now_ms)


def plus_dagen(dk: str, n: int) -> str:
    return (date.fromisoformat(dk) + timedelta(days=n)).isoformat()


def grenzen(dk: str) -> tuple[int, int]:
    d = date.fromisoformat(dk)
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    eind_d = d + timedelta(days=1)
    eind = datetime(eind_d.year, eind_d.month, eind_d.day, tzinfo=TZ)
    return int(start.timestamp() * 1000), int(eind.timestamp() * 1000)


def iso_ams(t: int) -> str:
    return datetime.fromtimestamp((t // 1000), TZ).isoformat(timespec="seconds")


def maanden(van: str, tot: str) -> list[str]:
    out, y, m = [], int(van[:4]), int(van[5:7])
    while True:
        k = f"{y}-{m:02d}"
        if k > tot:
            return out
        out.append(k)
        m += 1
        if m > 12:
            m, y = 1, y + 1


# ── Verdeling en integratie ─────────────────────────────────────────────
def verdeel(po: float, bo: float) -> dict[str, float]:
    """Huis krijgt zon voorrang; accu laadt eerst zonne-overschot, dan net.

    po > 0 is afname, bo > 0 is ontladen. rest = huis − zon (zonder accu).
    """
    ont, lad, rest = max(bo, 0.0), max(-bo, 0.0), po + bo
    ont_huis = min(ont, max(rest, 0.0))
    lad_pv = min(lad, max(-rest, 0.0))
    return {"ontHuis": ont_huis, "ontNet": ont - ont_huis, "ladPV": lad_pv, "ladNet": lad - lad_pv}


def gat_grens(res_ms: float | None) -> float:
    return max(120_000, min(round((res_ms or 0) * 1.5), 600_000))


def leeg_kwartier() -> dict[str, float]:
    return {"imp": 0.0, "exp": 0.0, "dek": 0.0, "ontHuis": 0.0, "ontNet": 0.0, "ladNet": 0.0, "ladPV": 0.0, "accuSec": 0.0}


def integreer_segment(kw: dict[int, dict], r: dict, n: dict) -> None:
    """Tel het segment r → n tijdgewogen bij in de kwartieren (gaten nooit opvullen)."""
    dt = n["t"] - r["t"]
    if dt <= 0 or dt > gat_grens(r.get("r")):
        return
    pos = r["t"]
    while pos < n["t"]:
        q = (pos // Q) * Q
        stop = min(n["t"], q + Q)
        uur, sec = (stop - pos) / 3_600_000, (stop - pos) / 1000
        c = kw.setdefault(q, leeg_kwartier())
        if r["po"] > 0:
            c["imp"] += r["po"] * uur / 1000
        else:
            c["exp"] += -r["po"] * uur / 1000
        c["dek"] += sec
        if getal(r.get("bo")):
            v = verdeel(r["po"], r["bo"])
            for f in FLOWS:
                c[f] += v[f] * uur / 1000
            c["accuSec"] += sec
        s = n.get("s")
        if getal(s) and 0 <= s <= 100 and stop == n["t"]:
            c["soc"] = s
        pos = stop


def naar_kwartieren(rows: list[dict]) -> dict[int, dict]:
    s = sorted((r for r in rows if r and getal(r.get("t")) and getal(r.get("po"))), key=lambda r: r["t"])
    kw: dict[int, dict] = {}
    for i in range(len(s) - 1):
        integreer_segment(kw, s[i], s[i + 1])
    return kw


# ── Prijzen ─────────────────────────────────────────────────────────────
def allin(kaal: Any, is_export: bool, t: int, saldeert: bool) -> float | None:
    if not getal(kaal):
        return None
    jaar = int(dag_van(t)[:4])
    eb_ex = EB_EX.get(jaar)
    if eb_ex is None:
        return None
    eb = eb_ex * BTW
    if is_export:
        return kaal * BTW - OPSLAG + (eb if saldeert and jaar < 2027 else 0)
    return kaal * BTW + OPSLAG + eb


def planning(j: dict | None) -> dict:
    plans = (j or {}).get("dayAheadPlans")
    windows = (j or {}).get("dayAheadUnavailableWindows")
    planned = {p["start"] for p in (plans if isinstance(plans, list) else [])
               if isinstance(p, dict) and getal(p.get("start")) and p.get("direction") == "import"}
    w = [x for x in (windows if isinstance(windows, list) else [])
         if isinstance(x, list) and len(x) >= 2 and getal(x[0]) and getal(x[1]) and x[1] > x[0]]
    return {"authoritative": isinstance(plans, list) or isinstance(windows, list), "planned": planned,
            "onbekend": lambda s: any(x[0] <= s < x[1] for x in w)}


LEGE_PLANNING = {"authoritative": False, "planned": set(), "onbekend": lambda s: False}


def prijs_keuze(p: dict, ctx: dict, events: set, disc: dict) -> dict:
    values = p.get("values") or {}
    if ctx["authoritative"]:
        state = "planned" if p["start"] in ctx["planned"] else "unavailable" if ctx["onbekend"](p["start"]) else "none"
    elif p.get("dayAheadState") in ("planned", "unavailable", "none"):
        state = p["dayAheadState"]
    else:
        state = "unavailable" if any(k.startswith("day_ahead_") for k in values) else "none"
    dirs = disc.get(p["start"], p.get("directions"))
    vlag = bool(p.get("afrr")) or p["start"] in events
    ai = ("import" in dirs) if isinstance(dirs, list) else vlag
    ae = ("export" in dirs) if isinstance(dirs, list) else vlag
    if state == "unavailable":
        mi = None
    elif state == "planned":
        mi = "day_ahead_afrr_import" if ai else "day_ahead_import"
    else:
        mi = "afrr_import" if ai else "onbalans_import"
    me = "afrr_export" if ae else "onbalans_export"
    ki = values.get(mi) if mi and getal(values.get(mi)) else None
    ke = values.get(me) if getal(values.get(me)) else None
    return {"mi": mi, "me": me, "ki": ki, "ke": ke}


def verwerk_prijzen(arch: dict[int, dict], j: dict | None, now_ms: int) -> int:
    """Voeg een current-prices-antwoord samen in het archief. Geeft aantal rijen."""
    if not j or not isinstance(j.get("prices"), list):
        return 0
    ctx = planning(j)
    events = set(j.get("afrrEventIsps") or [])
    disc = {d["start"]: d["directions"] for d in (j.get("afrrDiscovery") or [])
            if isinstance(d, dict) and getal(d.get("start")) and isinstance(d.get("directions"), list)}
    n = 0
    for p in j["prices"]:
        if not isinstance(p, dict) or not getal(p.get("start")) or p["start"] % Q or p.get("carried"):
            continue
        keus, oud = prijs_keuze(p, ctx, events, disc), arch.get(int(p["start"])) or {}
        tijdelijk = bool(p.get("temporaryTariffs")) or (p["start"] <= now_ms < p["start"] + Q)
        arch[int(p["start"])] = {
            "mi": keus["mi"], "me": keus["me"],
            "ki": keus["ki"] if keus["ki"] is not None else (oud.get("ki") if oud.get("mi") == keus["mi"] else None),
            "ke": keus["ke"] if keus["ke"] is not None else (oud.get("ke") if oud.get("me") == keus["me"] else None),
            "v": bool(p.get("provisional")) or tijdelijk, "t": now_ms,
        }
        n += 1
    return n


def prijs_gaten(arch: dict[int, dict], start: int, tot: int) -> int:
    mist = 0
    for q in range(start, tot, Q):
        a = arch.get(q)
        if not a or (not getal(a.get("ki")) and a.get("mi")) or not getal(a.get("ke")):
            mist += 1
    return mist


def zet_prijzen(kw: dict[int, dict], prijzen: dict[int, dict]) -> dict[int, dict]:
    for q, x in kw.items():
        p = prijzen.get(q)
        if not getal(x.get("pIn")) and p:
            x["pIn"] = p.get("ki")
        if not getal(x.get("pUit")) and p:
            x["pUit"] = p.get("ke")
        if p and p.get("v"):
            x["prijsVoorlopig"] = True
    return kw


def prijs_betrouwbaar(p: Any, kwh: float) -> bool:
    return getal(p) and abs(p) <= MAX_KAAL and kwh >= MIN_KWH


def kies_prijzen(kw: dict[int, dict], bron: dict[int, dict], prijzen: dict[int, dict], rij: dict | None) -> dict[int, dict]:
    """Effectieve BE-prijs als betrouwbaar, anders marktprijs, anders daggemiddelde."""
    rij = rij or {}
    gi, gu = rij.get("avg_buy_price_eur_per_kwh"), rij.get("avg_sell_price_eur_per_kwh")
    gem_in = gi if getal(gi) and abs(gi) <= MAX_KAAL else None
    gem_uit = gu if getal(gu) and abs(gu) <= MAX_KAAL else None
    for q, x in kw.items():
        b, a = bron.get(q) or {}, prijzen.get(q)
        bin_, buit = "BE", "BE"
        x["pIn"] = b.get("pIn") if prijs_betrouwbaar(b.get("pIn"), b.get("imp", 0)) else None
        x["pUit"] = b.get("pUit") if prijs_betrouwbaar(b.get("pUit"), b.get("exp", 0)) else None
        if not getal(x["pIn"]) and a and getal(a.get("ki")) and abs(a["ki"]) <= MAX_KAAL:
            x["pIn"], bin_ = a["ki"], "markt"
            if a.get("v"):
                x["prijsVoorlopig"] = True
        if not getal(x["pUit"]) and a and getal(a.get("ke")) and abs(a["ke"]) <= MAX_KAAL:
            x["pUit"], buit = a["ke"], "markt"
            if a.get("v"):
                x["prijsVoorlopig"] = True
        if not getal(x["pIn"]) and gem_in is not None:
            x["pIn"], bin_, x["prijsGeschatIn"] = gem_in, "daggemiddelde", True
        if not getal(x["pUit"]) and gem_uit is not None:
            x["pUit"], buit, x["prijsGeschatUit"] = gem_uit, "daggemiddelde", True
        x["prijsBron"] = bin_ if bin_ == buit else f"in {bin_} / uit {buit}"
    return kw


# ── Waardering ──────────────────────────────────────────────────────────
def waardeer(kw: dict[int, dict], saldeert: bool, afrr_eur: float | None) -> dict:
    """Besparing en handel volgen de bestemming van de ontladen kWh;
    laadkosten worden naar rato van die kWh verdeeld."""
    t = {"ontHuis": 0.0, "ontNet": 0.0, "ladNet": 0.0, "ladPV": 0.0, "huis": 0.0, "net": 0.0, "kNet": 0.0,
         "kPV": 0.0, "imp": 0.0, "exp": 0.0, "geladen": 0.0, "ontladen": 0.0, "accuSec": 0.0, "prijsSec": 0.0,
         "zonderPrijs": 0, "voorlopigePrijs": 0, "kwartieren": 0, "geschattePrijs": 0}
    for q in sorted(kw):
        x = kw[q]
        t["kwartieren"] += 1
        t["imp"] += x.get("imp") or 0
        t["exp"] += x.get("exp") or 0
        if not (x.get("accuSec", 0) > 0):
            continue
        sec = min(900, x["accuSec"])
        t["accuSec"] += sec
        t["geladen"] += x["geladen"] if getal(x.get("geladen")) else x["ladNet"] + x["ladPV"]
        t["ontladen"] += x["ontladen"] if getal(x.get("ontladen")) else x["ontHuis"] + x["ontNet"]
        if x["ontHuis"] + x["ontNet"] + x["ladNet"] + x["ladPV"] < 1e-9:
            t["prijsSec"] += sec
            continue
        pi, pe = allin(x.get("pIn"), False, q, saldeert), allin(x.get("pUit"), True, q, saldeert)
        if ((x["ontHuis"] > 0 or x["ladNet"] > 0) and pi is None) or ((x["ontNet"] > 0 or x["ladPV"] > 0) and pe is None):
            t["zonderPrijs"] += 1
            continue
        t["prijsSec"] += sec
        if x.get("prijsVoorlopig"):
            t["voorlopigePrijs"] += 1
        for f in FLOWS:
            t[f] += x[f]
        t["huis"] += x["ontHuis"] * (pi or 0)
        t["net"] += x["ontNet"] * (pe or 0)
        t["kNet"] += x["ladNet"] * (pi or 0)
        t["kPV"] += x["ladPV"] * (pe or 0)
        # Alleen tellen als de geschatte prijs in dit kwartier ook echt nodig is.
        if (x.get("prijsGeschatIn") and (x["ontHuis"] > 0 or x["ladNet"] > 0)) or (
                x.get("prijsGeschatUit") and (x["ontNet"] > 0 or x["ladPV"] > 0)):
            t["geschattePrijs"] += 1
    ontl, kosten = t["ontHuis"] + t["ontNet"], t["kNet"] + t["kPV"]
    if ontl > 1e-9:
        aandeel = t["ontHuis"] / ontl
        t["besparing"] = t["huis"] - aandeel * kosten
        t["handel"] = t["net"] - (1 - aandeel) * kosten
    else:
        t["besparing"], t["handel"] = -t["kPV"], -t["kNet"]
    t["afrr"] = afrr_eur if getal(afrr_eur) else 0.0
    t["handel"] += t["afrr"]
    t["totaal"] = t["besparing"] + t["handel"]
    return t


def afrr_van(rij: dict | None) -> float | None:
    if not rij:
        return None
    a, b = rij.get("balance_energie_eur"), rij.get("deal_customer_share_eur")
    if not getal(a) and not getal(b):
        return None
    return (a if getal(a) else 0) + (b if getal(b) else 0)


# ── Definitieve BE-dag ──────────────────────────────────────────────────
class BronFout(Exception):
    """BE-dagbron niet bruikbaar."""


def _ts(v: Any) -> float:
    if getal(v):
        return v
    if isinstance(v, str) and re.search(r"(Z|[+-]\d{2}:\d{2})$", v):
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
    return math.nan


def parse_definitief(j: dict, rij: dict | None, dk: str) -> dict[int, dict]:
    lijst = ((j or {}).get("day_detail") or {}).get("intervals")
    start, eind = grenzen(dk)
    pr = (j or {}).get("pricing") or {}
    if not j or j.get("interval") != "day" or j.get("period") != dk or pr.get("include_vat") is not False or pr.get("include_delivery_cost") is not False:
        raise BronFout("BE bevestigt dag of kale prijzen niet")
    if not isinstance(lijst, list):
        raise BronFout("Geen kwartierdata")
    out: dict[int, dict] = {}
    laden = ontladen = 0.0
    leeg_accu = 0
    for v in lijst:
        q = _ts(v.get("timestamp"))
        if not getal(q) or q % Q or q < start or q >= eind or int(q) in out:
            raise BronFout("Onbekende of dubbele kwartiertijd")
        q = int(q)
        f = [x for x in ("grid_import_kwh", "grid_export_kwh") if not getal(v.get(x)) or v[x] < 0]
        if f:
            raise BronFout("Ontbrekend: " + ", ".join(f))
        for x in ("battery_charged_kwh", "battery_discharged_kwh"):
            if v.get(x) is not None and (not getal(v.get(x)) or v[x] < 0):
                raise BronFout("Ongeldig: " + x)
        bl = v["battery_charged_kwh"] if getal(v.get("battery_charged_kwh")) else 0.0
        bo = v["battery_discharged_kwh"] if getal(v.get("battery_discharged_kwh")) else 0.0
        leeg = not getal(v.get("battery_charged_kwh")) or not getal(v.get("battery_discharged_kwh"))
        if leeg:
            leeg_accu += 1
        laden += bl
        ontladen += bo
        pi, pu = v.get("effective_import_price_eur_per_kwh"), v.get("effective_export_price_eur_per_kwh")
        out[q] = {"imp": v["grid_import_kwh"], "exp": v["grid_export_kwh"], "laden": bl, "ontladen": bo, "leeg": leeg,
                  "pIn": pi if getal(pi) else None, "pUit": pu if getal(pu) else None}
    if len(out) != (eind - start) // Q:
        raise BronFout("Kwartieren dekken niet de hele dag")
    if rij and getal(rij.get("battery_charged_kwh")) and getal(rij.get("battery_discharged_kwh")) and (
            abs(laden - rij["battery_charged_kwh"]) > 0.15 or abs(ontladen - rij["battery_discharged_kwh"]) > 0.15):
        raise BronFout("Kwartieren sluiten niet aan op dagtotaal")
    return out


def verdeel_definitief(bron: dict[int, dict], cache: dict[int, dict] | None) -> dict:
    uit, uit_cache, geschat, aangevuld, ontbrekend = {}, 0, 0, 0, 0
    cache = cache or {}
    for q, v in bron.items():
        c = cache.get(q)
        # BE-kwartier zonder accuwaarden: aanvullen uit eigen metingen, anders als ongemeten tellen.
        if v.get("leeg"):
            m = {"imp": v["imp"], "exp": v["exp"], "accuSec": 0, "ladNet": 0.0, "ladPV": 0.0, "ontHuis": 0.0,
                 "ontNet": 0.0, "geladen": 0.0, "ontladen": 0.0}
            if c and c.get("accuSec", 0) >= 810:
                for f in FLOWS:
                    m[f] = c[f]
                m["geladen"], m["ontladen"], m["accuSec"] = c["ladNet"] + c["ladPV"], c["ontHuis"] + c["ontNet"], 900
                if getal(c.get("soc")):
                    m["soc"] = c["soc"]
                aangevuld += 1
            else:
                ontbrekend += 1
            uit[q] = m
            continue
        cl = (c["ladNet"] + c["ladPV"]) if c else 0
        co = (c["ontHuis"] + c["ontNet"]) if c else 0
        n = {"imp": v["imp"], "exp": v["exp"], "accuSec": 900, "geladen": v["laden"], "ontladen": v["ontladen"]}
        if c and getal(c.get("soc")):
            n["soc"] = c["soc"]
        if c and c.get("accuSec", 0) >= 810 and (cl > 0 or v["laden"] == 0) and (co > 0 or v["ontladen"] == 0):
            n["ladPV"] = v["laden"] * c["ladPV"] / cl if cl else 0.0
            n["ladNet"] = v["laden"] - n["ladPV"]
            n["ontHuis"] = v["ontladen"] * c["ontHuis"] / co if co else 0.0
            n["ontNet"] = v["ontladen"] - n["ontHuis"]
            uit_cache += 1
        else:
            netto = v["ontladen"] - v["laden"]
            if abs(netto) < 1e-9:
                netto = 0.0
            ont, lad, rest = max(netto, 0.0), max(-netto, 0.0), v["imp"] - v["exp"] + netto
            n["ladPV"] = min(lad, max(-rest, 0.0))
            n["ladNet"] = lad - n["ladPV"]
            n["ontHuis"] = min(ont, max(rest, 0.0))
            n["ontNet"] = ont - n["ontHuis"]
            geschat += 1
        uit[q] = n
    return {"kw": uit, "uitCache": uit_cache, "geschat": geschat, "aangevuld": aangevuld, "ontbrekend": ontbrekend}


# ── Payloads en afstemmingslabel ────────────────────────────────────────
# Beide verzenders zetten een label achteraan de notitie:
#   [BEMB v=1.1.0 bron=ha|browser type=be|cache dekking=66.7 actief=<ISO>]
# 'actief' staat alleen in labels van Home Assistant (heartbeat).
LABEL_RE = re.compile(r"\[BEMB ([^\]]*)\]")


def maak_label(versie: str, bron: str, type_: str, dekking: float, actief: str | None = None) -> str:
    deel = f"v={versie} bron={bron} type={type_} dekking={to_fixed(100 * dekking, 1)}"
    if actief:
        deel += f" actief={actief}"
    return f"[BEMB {deel}]"


def lees_label(note: Any) -> dict[str, str] | None:
    m = LABEL_RE.search(note or "") if isinstance(note, str) else None
    if not m:
        return None
    return dict(x.split("=", 1) for x in m.group(1).split() if "=" in x)


def intervallen(kw: dict[int, dict]) -> list[dict]:
    out = []
    for q in sorted(kw)[:100]:
        x = kw[q]
        o = {"start": iso_ams(q),
             "batteryChargedKwh": k4(x["geladen"] if getal(x.get("geladen")) else x["ladNet"] + x["ladPV"]),
             "batteryDischargedKwh": k4(x["ontladen"] if getal(x.get("ontladen")) else x["ontHuis"] + x["ontNet"]),
             "gridImportedKwh": k4(x.get("imp")), "gridExportedKwh": k4(x.get("exp"))}
        if getal(x.get("soc")):
            o["batteryEndSoc"] = to_fixed(x["soc"], 1)
        out.append(o)
    return out


def dag_payload(r: dict, saldeert: bool, afrr: bool, versie: str, bron_label: str, actief: str | None = None) -> dict:
    """r: {dk, bron: 'BE'|'cache', kw, w, dekking, prijsDekking}; bron_label: 'ha'|'browser'."""
    w = r["w"]
    if r["bron"] == "cache":
        invalid = r["dekking"] < 0.99 or r["prijsDekking"] < 0.99 or w["zonderPrijs"] > 0
        kop = ("Voorlopig uit HA-metingen" if bron_label == "ha" else "Voorlopig uit browsercache") + f", dekking {to_fixed(100 * r['dekking'], 1)}%"
    else:
        invalid = w["zonderPrijs"] > 0 or (w.get("geschattePrijs") or 0) > 8
        kop = "BE definitief" + (f", {r['aangevuld']} kwartieren accuwaarden uit eigen metingen" if r.get("aangevuld") else "")
    note = (kop + (f". {w['geschattePrijs']} kwartieren met daggemiddelde prijs" if w.get("geschattePrijs") else "")
            + ". All-in" + (", EB op teruglevering t/m 2026" if saldeert else ", zonder EB op teruglevering")
            + ". Besparing = ontladen naar huis minus evenredige laadkosten; handel = ontladen naar net minus evenredige laadkosten"
            + (" plus aFRR-vergoeding" if r["bron"] == "BE" and afrr else "") + ".")
    label = maak_label(versie, bron_label, "be" if r["bron"] == "BE" else "cache", r["dekking"], actief)
    note = note[: 480 - len(label) - 1] + " " + label
    return {"date": r["dk"], "batteryResult": e2(w["totaal"]), "batterySavings": e2(w["besparing"]),
            "chargedToday": k3(w["geladen"]), "dischargedToday": k3(w["ontladen"]), "invalid": bool(invalid),
            "note": note, "intervals": intervallen(r["kw"])}


def payload_hash(p: dict) -> str:
    """Hash zonder de heartbeat, zodat alleen echte wijzigingen tellen."""
    q = dict(p)
    q.pop("test", None)
    q["note"] = re.sub(r" actief=[^\]\s]+", "", q.get("note", ""))
    return json.dumps(q, sort_keys=True, separators=(",", ":"))


def mag_overschrijven(bestaand: dict | None, nieuw_type: str, nieuw_dekking: float) -> tuple[bool, str]:
    """Afstemmingsregel voor een dagrecord op Mijnbatterij (gelijk in plugin en HA).

    - BE-definitief wordt nooit vervangen door een voorlopige (cache) versie.
    - Voorlopig vervangt voorlopig alleen bij minstens gelijke dekking.
    - Records zonder label (oude versies) mogen worden vervangen.
    """
    lab = lees_label((bestaand or {}).get("note"))
    if not lab:
        return True, ""
    if lab.get("type") == "be" and nieuw_type != "be":
        return False, "definitieve versie staat er al"
    if lab.get("type") == "cache" and nieuw_type == "cache":
        try:
            oud = float(lab.get("dekking", "0"))
        except ValueError:
            oud = 0.0
        if oud > 100 * nieuw_dekking + 0.05:
            return False, f"bestaande versie heeft meer dekking ({oud:.1f}%)"
    return True, ""
