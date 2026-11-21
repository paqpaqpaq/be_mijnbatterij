"""Dynamisch tarief voor geplande importkwartieren: kale EPEX (Nord Pool NL), gelijk aan All-in 6.8.3."""
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.be_mijnbatterij import api, calc
from custom_components.be_mijnbatterij.engine import Engine

# Echte Nord Pool-waarden 21-11-2026 00:00–04:00 UTC (01:00–05:00 NL), €/MWh.
NP = [("00:00", 186.99), ("00:15", 179.81), ("00:30", 177.17), ("00:45", 174.51), ("01:00", 179), ("01:15", 176.59),
      ("01:30", 174.66), ("01:45", 173.49), ("02:00", 174.66), ("02:15", 173.15), ("02:30", 173.21), ("02:45", 172.03),
      ("03:00", 172.6), ("03:15", 172.44), ("03:30", 171.35), ("03:45", 169.28)]
# Wat All-in 6.8.3 voor deze kwartieren in zijn archief zette (day_ahead_import; om 03:15/03:30 ook aFRR).
ALLIN = [0.18699, 0.17981, 0.17717, 0.17451, 0.179, 0.17659, 0.17466, 0.17349, 0.17466, 0.17315, 0.17321, 0.17203,
         0.1726, 0.17244, 0.17135, 0.16928]


def _q(hhmm: str) -> int:
    return calc.grenzen("2026-11-21")[0] + 3600_000 + (int(hhmm[:2]) * 60 + int(hhmm[3:])) * 60_000


def body(dk="2026-11-21"):
    def iso(ms):
        import datetime as dt
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"deliveryDateCET": dk, "market": "DayAhead", "deliveryAreas": ["NL"], "currency": "EUR",
            "multiAreaEntries": [{"deliveryStart": iso(_q(t)), "deliveryEnd": iso(_q(t) + calc.Q), "entryPerArea": {"NL": v}} for t, v in NP]}


def test_nordpool_gelijk_aan_allin():
    rows = calc.nordpool_rijen(body())
    assert [round(rows[_q(t)], 5) for t, _ in NP] == ALLIN


def test_vul_alleen_geplande_kwartieren_zonder_prijs():
    epex = calc.nordpool_rijen(body())
    q = [_q(t) for t, _ in NP]
    arch = {
        q[0]: {"mi": "day_ahead_import", "me": "onbalans_export", "ki": None, "ke": 0.2298, "vi": False},
        q[9]: {"mi": "day_ahead_afrr_import", "me": "afrr_export", "ki": None, "ke": None, "vi": False},
        q[1]: {"mi": "day_ahead_import", "me": "onbalans_export", "ki": 0.25, "vi": True},  # BE gaf zelf een prijs
        q[2]: {"mi": "onbalans_import", "me": "onbalans_export", "ki": None, "vi": False},  # niet gepland
    }
    assert calc.vul_dag_ahead(arch, epex) == 2
    assert arch[q[0]]["ki"] == pytest.approx(0.18699) and arch[q[0]]["vi"] and arch[q[0]]["da"] == "epex"
    assert arch[q[0]]["ke"] == 0.2298  # export ongemoeid
    assert arch[q[9]]["ki"] == pytest.approx(0.17315)
    assert arch[q[1]]["ki"] == 0.25 and arch[q[2]]["ki"] is None
    # Volgende BE-ronde met weer null: de EPEX-prijs blijft staan (zelfde markt).
    j = {"prices": [{"start": q[0], "values": {"day_ahead_import": None, "onbalans_export": 0.2298}, "dayAheadState": "planned"}]}
    calc.verwerk_prijzen(arch, j, q[0] + 3600_000, {})
    assert arch[q[0]]["ki"] == pytest.approx(0.18699)


def test_metTennet_laat_dag_ahead_staan():
    q = _q("00:00")
    arch = {q: {"mi": "day_ahead_import", "me": "onbalans_export", "ki": 0.18699, "vi": True, "ke": None}}
    uit = calc.met_tennet(arch, {q: {"ki": 0.23, "ke": 0.23}})
    assert uit[q]["ki"] == 0.18699 and uit[q]["ke"] == 0.23


@pytest.fixture
async def nordpool(socket_enabled, monkeypatch):
    log = []

    async def route(req):
        log.append(req.query.get("date"))
        return web.json_response(body(req.query.get("date")))
    app = web.Application(); app.router.add_get("/api/DayAheadPrices", route)
    srv = TestServer(app, host="127.0.0.1"); await srv.start_server()
    monkeypatch.setattr(calc, "NORDPOOL_URL", f"http://127.0.0.1:{srv.port}/api/DayAheadPrices")
    yield log
    await srv.close()


async def test_engine_vult_en_onthoudt(nordpool):
    async with aiohttp.ClientSession() as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, epex=api.EpexClient(s))
        q = _q("01:30")
        arch = {q: {"mi": "day_ahead_import", "me": "onbalans_export", "ki": None, "vi": False}}
        assert await e._vul_epex(arch) == 1 and arch[q]["ki"] == pytest.approx(0.17466)
        assert nordpool == ["2026-11-21"]
        arch2 = {q: {"mi": "day_ahead_import", "ki": None}}
        await e._vul_epex(arch2)
        assert len(nordpool) == 1  # dag in opslag, geen tweede verzoek
