"""TenneT-settlementprijzen: ophalen (APEX-sessie), omzetten naar kwartieren en voorrang in de prijskeuze."""
import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.be_mijnbatterij import api, calc
from custom_components.be_mijnbatterij.engine import Engine

from . import fake_servers as fs


def test_wintertijd_100_kwartieren():
    rijen = {i: [100.0, 90.0] for i in range(1, 101)}
    rows, compleet = calc.tennet_kwartieren("2026-10-25", rijen)
    a, b = calc.grenzen("2026-10-25")
    assert len(rows) == 100 and compleet and min(rows) == a and max(rows) == b - calc.Q
    assert rows[a + 12 * calc.Q] == {"ki": 0.1, "ke": 0.09}


def test_onvolledig_niet_compleet():
    rows, compleet = calc.tennet_kwartieren("2026-06-15", {1: [100.0, 50.0], 2: [None, 40.0]})
    assert len(rows) == 2 and not compleet


def test_met_tennet_voorrang():
    q = calc.grenzen("2026-06-15")[0]
    tn = {q: {"ki": 0.2, "ke": -0.5}, q + calc.Q: {"ki": 0.3, "ke": 0.1}, q + 2 * calc.Q: {"ki": 0.4, "ke": 0.2}}
    arch = {
        # Tenergy-schatting, niet vastgesteld → TenneT
        q: {"mi": "onbalans_import", "me": "onbalans_export", "ki": 0.25, "ke": 0.25, "vi": False, "ve": False, "ti": True, "te": True},
        # day-ahead en aFRR van BE blijven staan
        q + calc.Q: {"mi": "day_ahead_import", "me": "afrr_export", "ki": 0.05, "ke": 0.9, "vi": True, "ve": True},
    }
    uit = calc.met_tennet(arch, tn)
    assert uit[q]["ki"] == 0.2 and uit[q]["ke"] == -0.5 and uit[q]["vi"] and uit[q]["tnE"]
    assert uit[q + calc.Q]["ki"] == 0.05 and uit[q + calc.Q]["ke"] == 0.9 and "tnI" not in uit[q + calc.Q]
    assert uit[q + 2 * calc.Q]["ki"] == 0.4 and uit[q + 2 * calc.Q]["mi"] == "onbalans_import"
    assert arch[q]["ki"] == 0.25  # origineel onaangeroerd


def test_settlementprijs_boven_150_telt():
    q = calc.grenzen("2026-06-15")[0]
    kw = {q: {"ontNet": 1.75, "ladPV": 0.0, "ontHuis": 0.0, "ladNet": 0.0}}
    bron = {q: {"imp": 0.002, "exp": 1.752, "mUit": 3.5, "mIn": 3.5, "pIn": None, "pUit": 3.5}}
    x = calc.kies_prijzen(kw, bron, {}, {"avg_sell_price_eur_per_kwh": 0.1})[q]
    assert x["pUit"] == 3.5 and x["pUitVast"]
    # Ook een vastgestelde archief-/TenneT-prijs boven € 1,50 blijft staan; een niet-vastgestelde niet.
    x = calc.kies_prijzen({q: dict(kw[q])}, {q: {"imp": 0, "exp": 0}}, {q: {"ke": 4.2, "ve": True, "tnE": True}}, None)[q]
    assert x["pUit"] == 4.2 and x["prijsBron"].endswith("TenneT")
    x = calc.kies_prijzen({q: dict(kw[q])}, {q: {"imp": 0, "exp": 0}}, {q: {"ke": 4.2, "ve": False}}, None)[q]
    assert x["pUit"] is None


@pytest.fixture
async def server(socket_enabled, monkeypatch):
    srv = TestServer(fs.maak_app(), host="127.0.0.1")
    await srv.start_server()
    monkeypatch.setattr(calc, "TENNET_URL", f"http://127.0.0.1:{srv.port}/ords/")
    fs.tennet_log.clear()
    yield srv
    await srv.close()


async def test_ophalen_met_sessiecookie(server):
    # Zoals de gedeelde HA-sessie: de cookie moet expliciet mee (de jar slaat niets op voor een IP-adres).
    async with aiohttp.ClientSession() as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, tennet=api.TennetClient(s))
        gister = calc.plus_dagen(calc.vandaag(e.now_ms()), -1)
        rows = await e.tennet_prijzen(gister)
        a, b = calc.grenzen(gister)
        assert len(rows) == (b - a) // calc.Q
        assert rows[a]["ki"] == pytest.approx(0.1) and rows[a + 40 * calc.Q]["ke"] == pytest.approx(3.5)
        assert ("ajax", "ORA_WWV_APP_100=SESSIE123") in fs.tennet_log
        assert st["tennet"][gister]["compleet"]
        # Complete dag komt uit de opslag: geen nieuw verzoek.
        n = len(fs.tennet_log)
        await e.tennet_prijzen(gister)
        assert len(fs.tennet_log) == n
        # Vandaag: nooit ophalen (pas na afloop gepubliceerd).
        assert await e.tennet_prijzen(calc.vandaag(e.now_ms())) == {}
        assert len(fs.tennet_log) == n
        # Prijzen per dag: archief + TenneT.
        p = await e.prijzen_dag(gister)
        assert p[a]["tnI"] and p[a]["vi"]


async def test_sessie_geweigerd_geeft_melding(server, monkeypatch):
    async def zonder_cookie(req):
        return fs.web.Response(text="<!DOCTYPE html><html>sessie verlopen</html>", content_type="text/html")
    async with aiohttp.ClientSession() as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, tennet=api.TennetClient(s))
        monkeypatch.setattr(calc, "TENNET_URL", f"http://127.0.0.1:{server.port}/bestaatniet/")
        gister = calc.plus_dagen(calc.vandaag(e.now_ms()), -1)
        assert await e.tennet_prijzen(gister) == {}
        assert "fout" in st["tennet"][gister] and any("TenneT-prijzen" in r for r in e.log)
