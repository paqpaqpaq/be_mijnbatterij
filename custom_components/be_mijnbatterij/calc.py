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
from datetime import date, datetime, time as dtime, timedelta
from html.parser import HTMLParser
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


# ── Omvormerrendement ───────────────────────────────────────────────────
# BE's accuvermogen (bo) is DC. Gewaardeerd wordt de AC-kant: ontladen AC =
# DC × η_uit, laden AC = DC / η_in. η per installatie gemeten op momenten
# zonder PV (p, g ≈ 0), waar accu AC = huis − net exact is.
ETA_STANDAARD = {"uit": [0.945, 0.945, 0.881], "in": [0.957, 0.957, 0.957]}


def eta_klasse(w: float, laden: bool) -> int:
    a = abs(w)
    if laden:
        return 0 if a < 3000 else 1 if a < 7000 else 2
    return 0 if a < 2000 else 1 if a < 5000 else 2


def naar_ac(bo: float, e: dict | None = None) -> float:
    e = e or ETA_STANDAARD
    if bo > 0:
        return bo * e["uit"][eta_klasse(bo, False)]
    if bo < 0:
        return bo / e["in"][eta_klasse(bo, True)]
    return 0.0


def kalibratie_leeg() -> dict:
    return {"uit": [[0.0, 0.0, 0] for _ in range(3)], "in": [[0.0, 0.0, 0] for _ in range(3)]}


def kalibratie_tel(som: dict, r: dict) -> None:
    """Tel één meting mee (alleen zonder PV en bij > 800 W accuvermogen)."""
    if not all(getal(r.get(f)) for f in ("po", "bo", "l", "p", "g")) or r["p"] >= 15 or r["g"] >= 15 or abs(r["bo"]) <= 800:
        return
    laden = r["bo"] < 0
    cel = som["in" if laden else "uit"][eta_klasse(r["bo"], laden)]
    if laden:
        cel[0] += -r["bo"]; cel[1] += r["po"] - r["l"]
    else:
        cel[0] += r["l"] - r["po"]; cel[1] += r["bo"]
    cel[2] += 1


def kalibratie_eta(som: dict) -> dict:
    """Klasse vanaf 50 metingen, anders richtingsgemiddelde vanaf 100, anders standaard."""
    uit: dict = {}
    for naam in ("uit", "in"):
        t0 = sum(c[0] for c in som[naam]); t1 = sum(c[1] for c in som[naam]); tn = sum(c[2] for c in som[naam])
        totaal = t0 / t1 if tn >= 100 and t1 > 0 else None
        waarden = []
        for i, c in enumerate(som[naam]):
            v = c[0] / c[1] if c[2] >= 50 and c[1] > 0 else totaal
            waarden.append(round(v * 1000) / 1000 if v is not None and 0.8 <= v <= 0.99 else ETA_STANDAARD[naam][i])
        uit[naam] = waarden
    uit["n"] = {naam: [c[2] for c in som[naam]] for naam in ("uit", "in")}
    return uit


def kalibreer(rows: list[dict]) -> dict:
    som = kalibratie_leeg()
    for r in rows or []:
        if isinstance(r, dict):
            kalibratie_tel(som, r)
    return kalibratie_eta(som)


def eta_tekst(e: dict) -> str:
    return ("Omvormerrendement uit " + "/".join(to_fixed(x, 2) for x in e["uit"])
            + ", in " + "/".join(to_fixed(x, 2) for x in e["in"]))


def gat_grens(res_ms: float | None) -> float:
    return max(120_000, min(round((res_ms or 0) * 1.5), 600_000))


def leeg_kwartier() -> dict[str, float]:
    return {"imp": 0.0, "exp": 0.0, "dek": 0.0, "ontHuis": 0.0, "ontNet": 0.0, "ladNet": 0.0, "ladPV": 0.0, "accuSec": 0.0,
            "geladen": 0.0, "ontladen": 0.0}


def integreer_segment(kw: dict[int, dict], r: dict, n: dict, e: dict | None = None) -> None:
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
            # Verdeling en waardering in AC; geladen/ontladen blijven de gemeten DC-kWh.
            v = verdeel(r["po"], naar_ac(r["bo"], e))
            for f in FLOWS:
                c[f] += v[f] * uur / 1000
            c["geladen"] = c.get("geladen", 0.0) + max(-r["bo"], 0) * uur / 1000
            c["ontladen"] = c.get("ontladen", 0.0) + max(r["bo"], 0) * uur / 1000
            c["accuSec"] += sec
        s = n.get("s")
        if getal(s) and 0 <= s <= 100 and stop == n["t"]:
            c["soc"] = s
        pos = stop


def naar_kwartieren(rows: list[dict], e: dict | None = None) -> dict[int, dict]:
    s = sorted((r for r in rows if r and getal(r.get("t")) and getal(r.get("po"))), key=lambda r: r["t"])
    kw: dict[int, dict] = {}
    for i in range(len(s) - 1):
        integreer_segment(kw, s[i], s[i + 1], e)
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


# ── Onbalansprijzen via Tenergy ─────────────────────────────────────────
# Sinds 17-11-2026 levert BE (current-prices) alleen nog prijzen van kwartieren waarin
# de accu iets deed. Net als Balansenergie All-in vullen we onbalans_import/-export aan
# uit de publieke Tenergy-pagina: minuutwaarden "Afnemen"/"Invoeden" (€/MWh), per
# kwartier volgens de regeltoestand uit de activatietabel. Rangorde: BE > Tenergy.
TENERGY_URL = "https://services.tenergy.nl/public.aspx/actualimbalanceprices"
MINUUT = 60_000
TENERGY_MIN_DEKKING = 10
_TENERGY_DATA = re.compile(r"data\s*=\s*(\{[\s\S]*?\})\s*;\s*serialChart\.updateChart")


def _tenergy_reeks(data: dict, ident: str) -> dict[int, float]:
    s = next((x for x in (data.get("ChartSeries") or []) if isinstance(x, dict) and x.get("Id") == ident), None)
    if not s or not isinstance(s.get("Data"), list):
        raise ValueError(f"Tenergy-reeks {ident} ontbreekt")
    uit: dict[int, float] = {}
    for p in s["Data"]:
        m = re.search(r"-?\d+", str((p or {}).get("Key")))
        try:
            v = float((p or {}).get("Value"))
        except (TypeError, ValueError):
            continue
        if m and int(m.group(0)) % MINUUT == 0 and math.isfinite(v):
            uit[int(m.group(0))] = v
    return uit


class _TabelLezer(HTMLParser):
    """Rijen van <table class="imbalanceprices"> als lijsten celteksten."""

    def __init__(self) -> None:
        super().__init__()
        self.rijen: list[list[str]] = []
        self._diepte = 0
        self._rij: list[str] | None = None
        self._cel: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._diepte or "imbalanceprices" in (dict(attrs).get("class") or "").split():
                self._diepte += 1
        elif self._diepte and tag == "tr":
            self._rij = []
        elif self._diepte and tag in ("td", "th") and self._rij is not None:
            self._cel = []

    def handle_endtag(self, tag):
        if tag == "table" and self._diepte:
            self._diepte -= 1
        elif tag in ("td", "th") and self._cel is not None and self._rij is not None:
            self._rij.append("".join(self._cel).strip())
            self._cel = None
        elif tag == "tr" and self._rij is not None:
            self.rijen.append(self._rij)
            self._rij = None

    def handle_data(self, data):
        if self._cel is not None:
            self._cel.append(data)


def _nl_getal(t: str) -> float | None:
    try:
        return float(str(t).strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def tenergy_activatie(html: str, nu: int, act: dict) -> dict:
    """Minuutactivatie [op, af, nood op, nood af] (MW) uit de tabel; tabeltijd is NL-tijd."""
    lezer = _TabelLezer()
    lezer.feed(html)
    dag = dag_van(nu)
    for c in lezer.rijen:
        if len(c) < 9 or not re.fullmatch(r"\d{1,2}:\d{2}", c[2]):
            continue
        vals = [_nl_getal(x) for x in c[3:7]]
        if any(v is None for v in vals):
            continue
        uur, minuut = (int(x) for x in c[2].split(":"))
        t = int(datetime.combine(date.fromisoformat(dag), dtime(uur, minuut), TZ).timestamp() * 1000)
        if t > nu + MINUUT:
            t -= 86_400_000
        act[str(t)] = vals
    grens = nu - 2 * 86_400_000
    for k in [k for k in act if int(k) < grens]:
        del act[k]
    return act


def tenergy_kwartier(q: int, afnemen: dict, invoeden: dict, act: dict, nu: int) -> dict | None:
    minuten = [t for t in range(q, min(q + Q, nu + 1), MINUUT) if t in afnemen and t in invoeden]
    if not minuten:
        return None
    laatste = minuten[-1]
    res = {"ki": afnemen[laatste] / 1000, "ke": invoeden[laatste] / 1000, "minuut": laatste, "rs": None}
    met = [t for t in minuten if str(t) in act]
    if len(met) < TENERGY_MIN_DEKKING:
        return res
    op = [t for t in met if act[str(t)][0] > 0 or act[str(t)][2] > 0]
    af = [t for t in met if act[str(t)][1] > 0 or act[str(t)][3] > 0]
    if op and af:
        res.update(rs=2, ki=max(afnemen[t] for t in minuten) / 1000, ke=min(invoeden[t] for t in minuten) / 1000)
    elif af:
        v = min([afnemen[t] for t in af] + [invoeden[t] for t in af]) / 1000
        res.update(rs=-1, ki=v, ke=v)
    elif op:
        v = max([afnemen[t] for t in op] + [invoeden[t] for t in op]) / 1000
        res.update(rs=1, ki=v, ke=v)
    else:
        res["rs"] = 0
    return res


def tenergy_verwerk(html: str, nu: int, kwartieren: dict, act: dict, bewaar_ms: int = 3 * 86_400_000) -> int:
    """Tenergy-pagina samenvoegen in kwartieren {str(q): {ki, ke, klaar, rs}}. Geeft aantal."""
    m = _TENERGY_DATA.search(html or "")
    if not m:
        raise ValueError("Tenergy-pagina zonder grafiekdata")
    data = json.loads(m.group(1))
    afnemen, invoeden = _tenergy_reeks(data, "PRICES_TAKE"), _tenergy_reeks(data, "PRICES_FEED")
    tenergy_activatie(html, nu, act)
    aantal = 0
    for q in sorted({t - t % Q for t in afnemen if t <= nu}):
        r = tenergy_kwartier(q, afnemen, invoeden, act, nu)
        if not r:
            continue
        klaar = q + Q <= nu and r["minuut"] >= q + Q - MINUUT
        oud = kwartieren.get(str(q))
        if oud and oud.get("klaar") and not klaar:
            continue
        if oud and oud.get("klaar") and oud.get("rs") is not None and r["rs"] is None:
            continue
        kwartieren[str(q)] = {"ki": r["ki"], "ke": r["ke"], "klaar": klaar, "rs": r["rs"]}
        aantal += 1
    if not aantal:
        raise ValueError("Tenergy gaf geen bruikbare minuutwaarden")
    for k in [k for k in kwartieren if int(k) < nu - bewaar_ms]:
        del kwartieren[k]
    return aantal


def vul_onbalans(values: dict, t: dict | None) -> dict:
    """BE-waarden aanvullen met Tenergy waar BE geen onbalansprijs gaf."""
    v = dict(values or {})
    if t:
        if not getal(v.get("onbalans_import")) and getal(t.get("ki")):
            v["onbalans_import"] = t["ki"]
        if not getal(v.get("onbalans_export")) and getal(t.get("ke")):
            v["onbalans_export"] = t["ke"]
    return v


def verwerk_prijzen(arch: dict[int, dict], j: dict | None, now_ms: int, tenergy: dict | None = None) -> int:
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
        orig = p.get("values") or {}
        t = (tenergy or {}).get(str(int(p["start"])))
        tv, af = tenergy_vast(t, int(p["start"]), now_ms), p["start"] + Q <= now_ms
        p = {**p, "values": vul_onbalans(orig, t)}
        keus, oud = prijs_keuze(p, ctx, events, disc), arch.get(int(p["start"])) or {}
        if keus["ki"] is None and keus["ke"] is None and oud:
            continue  # BE gaf voor dit kwartier niets meer: eerder vastgelegde prijs houden
        tijdelijk = bool(p.get("temporaryTariffs")) or (p["start"] <= now_ms < p["start"] + Q)
        be_i = keus["ki"] is not None and getal(orig.get(keus["mi"]))
        be_e = keus["ke"] is not None and getal(orig.get(keus["me"]))
        houd_i = keus["ki"] is None and oud.get("mi") == keus["mi"]
        houd_e = keus["ke"] is None and oud.get("me") == keus["me"]
        arch[int(p["start"])] = {
            "mi": keus["mi"], "me": keus["me"],
            "ki": keus["ki"] if keus["ki"] is not None else (oud.get("ki") if houd_i else None),
            "ke": keus["ke"] if keus["ke"] is not None else (oud.get("ke") if houd_e else None),
            "v": bool(p.get("provisional")) or tijdelijk, "t": now_ms,
            "ti": (not be_i) if keus["ki"] is not None else (bool(oud.get("ti")) if houd_i else False),
            "te": (not be_e) if keus["ke"] is not None else (bool(oud.get("te")) if houd_e else False),
            "vi": (af if be_i else tv) if keus["ki"] is not None else (bool(oud.get("vi")) if houd_i else False),
            "ve": (af if be_e else tv) if keus["ke"] is not None else (bool(oud.get("ve")) if houd_e else False),
        }
        n += 1
    return n


def tenergy_vast(t: dict | None, q: int, now_ms: int) -> bool:
    """Vastgesteld = na afloop bepaald: door BE zelf (activiteit/aFRR), of door Tenergy met de
    regeltoestand uit de activatiedata, zoals het BE-dashboard. Zonder activatiedata is het
    alleen de prijs van de laatste minuut: bruikbaar, maar niet vastgesteld."""
    return bool(t and t.get("klaar") and t.get("rs") is not None and q + Q <= now_ms)


def tenergy_aanvullen(arch: dict[int, dict], tenergy: dict | None, now_ms: int, van_ms: int) -> int:
    """Kwartieren zonder (volledige) prijs aanvullen met Tenergy-onbalans, ook als BE de rij
    helemaal niet meer levert. Alleen onbalansvelden; aFRR/day-ahead blijven van BE."""
    n = 0
    for k, t in (tenergy or {}).items():
        q = int(k)
        if q < van_ms or q > now_ms or q % Q:
            continue
        a, tv = arch.get(q), tenergy_vast(t, q, now_ms)
        if not a:
            arch[q] = {"mi": "onbalans_import", "me": "onbalans_export",
                       "ki": t.get("ki") if getal(t.get("ki")) else None, "ke": t.get("ke") if getal(t.get("ke")) else None,
                       "v": not t.get("klaar") or q + Q > now_ms, "t": now_ms, "ti": True, "te": True,
                       "vi": tv and getal(t.get("ki")), "ve": tv and getal(t.get("ke"))}
            n += 1
            continue
        gewijzigd = False
        # Ontbreekt de prijs, of kwam hij eerder (nog niet vastgesteld) van Tenergy: bijwerken.
        if a.get("mi") == "onbalans_import" and getal(t.get("ki")) and (not getal(a.get("ki")) or (a.get("ti") and not a.get("vi"))):
            a.update(ki=t["ki"], ti=True, vi=tv); gewijzigd = True
        if a.get("me") == "onbalans_export" and getal(t.get("ke")) and (not getal(a.get("ke")) or (a.get("te") and not a.get("ve"))):
            a.update(ke=t["ke"], te=True, ve=tv); gewijzigd = True
        n += gewijzigd
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
            x["pIn"], x["pInVast"] = p.get("ki"), bool(p.get("vi"))
        if not getal(x.get("pUit")) and p:
            x["pUit"], x["pUitVast"] = p.get("ke"), bool(p.get("ve"))
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
        x["pInVast"], x["pUitVast"] = getal(x["pIn"]), getal(x["pUit"])
        if not getal(x["pIn"]) and a and getal(a.get("ki")) and abs(a["ki"]) <= MAX_KAAL:
            x["pIn"], x["pInVast"], bin_ = a["ki"], bool(a.get("vi")), "markt"
            if a.get("v"):
                x["prijsVoorlopig"] = True
        if not getal(x["pUit"]) and a and getal(a.get("ke")) and abs(a["ke"]) <= MAX_KAAL:
            x["pUit"], x["pUitVast"], buit = a["ke"], bool(a.get("ve")), "markt"
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
         "zonderPrijs": 0, "voorlopigePrijs": 0, "kwartieren": 0, "geschattePrijs": 0, "vastSec": 0, "onzeker": 0}
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
            t["vastSec"] += sec
            continue
        pi, pe = allin(x.get("pIn"), False, q, saldeert), allin(x.get("pUit"), True, q, saldeert)
        if ((x["ontHuis"] > 0 or x["ladNet"] > 0) and pi is None) or ((x["ontNet"] > 0 or x["ladPV"] > 0) and pe is None):
            t["zonderPrijs"] += 1
            continue
        t["prijsSec"] += sec
        if x.get("prijsVoorlopig"):
            t["voorlopigePrijs"] += 1
        # Alleen vastgestelde prijzen tellen voor de nauwkeurigheid (afstemming plugin ↔ HA).
        if ((x["ontHuis"] > 0 or x["ladNet"] > 0) and not x.get("pInVast")) or ((x["ontNet"] > 0 or x["ladPV"] > 0) and not x.get("pUitVast")):
            t["onzeker"] += 1
        else:
            t["vastSec"] += sec
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


def verdeel_definitief(bron: dict[int, dict], cache: dict[int, dict] | None, e: dict | None = None) -> dict:
    uit, uit_cache, geschat, geschat_actief, aangevuld, ontbrekend = {}, 0, 0, 0, 0, 0
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
                m["geladen"] = c["geladen"] if getal(c.get("geladen")) else 0.0
                m["ontladen"] = c["ontladen"] if getal(c.get("ontladen")) else 0.0
                m["accuSec"] = 900
                if getal(c.get("soc")):
                    m["soc"] = c["soc"]
                aangevuld += 1
            else:
                ontbrekend += 1
            uit[q] = m
            continue
        cl = (c["ladNet"] + c["ladPV"]) if c else 0
        co = (c["ontHuis"] + c["ontNet"]) if c else 0
        # BE-kWh zijn DC; omrekenen naar AC met het rendement bij het kwartiergemiddelde vermogen.
        lad_ac = -naar_ac(-v["laden"] * 4000, e) / 4000
        ont_ac = naar_ac(v["ontladen"] * 4000, e) / 4000
        n = {"imp": v["imp"], "exp": v["exp"], "accuSec": 900, "geladen": v["laden"], "ontladen": v["ontladen"]}
        if c and getal(c.get("soc")):
            n["soc"] = c["soc"]
        if c and c.get("accuSec", 0) >= 810 and (cl > 0 or v["laden"] == 0) and (co > 0 or v["ontladen"] == 0):
            n["ladPV"] = lad_ac * c["ladPV"] / cl if cl else 0.0
            n["ladNet"] = lad_ac - n["ladPV"]
            n["ontHuis"] = ont_ac * c["ontHuis"] / co if co else 0.0
            n["ontNet"] = ont_ac - n["ontHuis"]
            uit_cache += 1
        else:
            netto = ont_ac - lad_ac
            if abs(netto) < 1e-9:
                netto = 0.0
            ont, lad, rest = max(netto, 0.0), max(-netto, 0.0), v["imp"] - v["exp"] + netto
            n["ladPV"] = min(lad, max(-rest, 0.0))
            n["ladNet"] = lad - n["ladPV"]
            n["ontHuis"] = min(ont, max(rest, 0.0))
            n["ontNet"] = ont - n["ontHuis"]
            geschat += 1
            if v["laden"] > 0 or v["ontladen"] > 0:
                geschat_actief += 1
        uit[q] = n
    return {"kw": uit, "n": len(bron), "uitCache": uit_cache, "geschat": geschat, "geschatActief": geschat_actief,
            "aangevuld": aangevuld, "ontbrekend": ontbrekend}


def kwaliteit(d: dict, w: dict) -> float:
    """Nauwkeurigheid van een definitieve dag (0–1): aandeel kwartieren met verdeling uit eigen
    metingen en een echte prijs. Bepaalt welke verzender een BE-dag mag vervangen."""
    n = d.get("n") or 0
    if not n:
        return 0.0
    slecht = (d["geschatActief"] + d["ontbrekend"] + (w.get("geschattePrijs") or 0) + (w.get("zonderPrijs") or 0)
              + (w.get("onzeker") or 0))
    return max(0.0, min(1.0, (n - slecht) / n))


# ── Payloads en afstemmingslabel ────────────────────────────────────────
# Beide verzenders zetten een label achteraan de notitie:
#   [BEMB v=1.1.0 bron=ha|browser type=be|cache dekking=66.7 actief=<ISO>]
# 'actief' staat alleen in labels van Home Assistant (heartbeat).
LABEL_RE = re.compile(r"\[BEMB ([^\]]*)\]")


def maak_label(versie: str, bron: str, type_: str, dekking: float, actief: str | None = None,
               kwal: float | None = None) -> str:
    deel = f"v={versie} bron={bron} type={type_} dekking={to_fixed(100 * dekking, 1)}"
    if getal(kwal):
        deel += f" kwal={to_fixed(100 * kwal, 1)}"
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
            + (" plus aFRR-vergoeding" if r["bron"] == "BE" and afrr else "") + ". " + eta_tekst(r.get("eta") or ETA_STANDAARD) + ".")
    label = maak_label(versie, bron_label, "be" if r["bron"] == "BE" else "cache", r["dekking"], actief, r.get("kwal"))
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


# Marge (procentpunt) bij het vergelijken van dekking/nauwkeurigheid: twee verzenders met
# vrijwel volledige data mogen elkaar vervangen; de plugin vult pas aan bij +1 pp (geen pingpong).
TOLERANTIE = 0.5


def mag_overschrijven(bestaand: dict | None, nieuw_type: str, nieuw_dekking: float,
                      nieuw_kwal: float | None = None) -> tuple[bool, str]:
    """Afstemmingsregel voor een dagrecord op Mijnbatterij (gelijk in plugin en HA).

    - BE-definitief wordt nooit vervangen door een voorlopige (cache) versie.
    - Zelfde soort vervangt alleen bij minstens gelijke nauwkeurigheid (kwal): het aandeel
      van de dag met eigen meting én vastgestelde prijs. Labels zonder kwal tellen als 0.
    - Records zonder label (oude versies) mogen worden vervangen.
    """
    lab = lees_label((bestaand or {}).get("note"))
    if not lab:
        return True, ""
    if lab.get("type") == "be" and nieuw_type != "be":
        return False, "definitieve versie staat er al"
    if lab.get("type") == nieuw_type:
        try:
            oud_k = float(lab.get("kwal", "0"))
        except ValueError:
            oud_k = 0.0
        nk = 100 * (nieuw_kwal or 0)
        if oud_k > nk + TOLERANTIE:
            return False, f"bestaande versie is nauwkeuriger ({oud_k:.1f}% tegen {nk:.1f}%)"
    return True, ""
    if lab.get("type") == "be" and nieuw_type != "be":
        return False, "definitieve versie staat er al"
    if lab.get("type") == "be" and nieuw_type == "be":
        # Labels zonder kwal (vóór 1.2.2) tellen als 0: die mogen altijd worden verbeterd.
        try:
            oud_k = float(lab.get("kwal", "0"))
        except ValueError:
            oud_k = 0.0
        nk = 100 * (nieuw_kwal or 0)
        if oud_k > nk + TOLERANTIE:
            return False, f"bestaande definitieve versie is nauwkeuriger ({oud_k:.1f}% tegen {nk:.1f}%)"
    if lab.get("type") == "cache" and nieuw_type == "cache":
        try:
            oud = float(lab.get("dekking", "0"))
        except ValueError:
            oud = 0.0
        if oud > 100 * nieuw_dekking + TOLERANTIE:
            return False, f"bestaande versie heeft meer dekking ({oud:.1f}%)"
    return True, ""
