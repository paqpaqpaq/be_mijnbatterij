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


def test_tabel_echte_html():
    from .tennet_html import pagina, JUNI15
    t = calc.tennet_tabel(pagina("15-06-2026", JUNI15))
    assert t["datum"] == "15-06-2026" and not t["leeg"]
    assert t["rijen"][40] == [3500.0, 3500.0] and t["rijen"][60] == [-0.68, -0.68] and t["rijen"][2] == [108.17, 24.4]
    leeg = calc.tennet_tabel(pagina("20-11-2026", []))
    assert leeg["leeg"] and leeg["rijen"] == {}
    with pytest.raises(calc.BronFout):
        calc.tennet_tabel("<html><body>iets anders</body></html>")


def test_vast_rendement_overal_gelijk():
    assert calc.ETA_VAST["uit"] == [0.925] * 3 and calc.ETA_VAST["in"] == [0.969] * 3
    assert calc.eta_tekst(calc.ETA_VAST) == "Omvormerrendement uit 0.925, in 0.969"
    e = Engine(None, None, {}, {}, "test", slaap=lambda x: None)
    assert e.eta is calc.ETA_VAST  # niet de eigen meting


@pytest.fixture
async def server(socket_enabled, monkeypatch):
    srv = TestServer(fs.maak_app(), host="127.0.0.1")
    await srv.start_server()
    monkeypatch.setattr(calc, "TENNET_URL", f"http://127.0.0.1:{srv.port}/ords/")
    fs.tennet_log.clear()
    fs.tennet_modus.update(modus="ok", mist=set())
    yield srv
    await srv.close()


async def test_ophalen_een_get_zonder_cookie(server):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, tennet=api.TennetClient(s))
        gister = calc.plus_dagen(calc.vandaag(e.now_ms()), -1)
        rows = await e.tennet_prijzen(gister)
        a, b = calc.grenzen(gister)
        assert len(rows) == (b - a) // calc.Q
        assert rows[a]["ki"] == pytest.approx(0.1) and rows[a + 39 * calc.Q]["ki"] == pytest.approx(3.5)
        assert rows[a + 59 * calc.Q]["ke"] == pytest.approx(-0.00068)
        assert len(fs.tennet_log) == 1 and "P90035_PTU_DATE_LOC:" in fs.tennet_log[0]
        assert st["tennet"][gister]["compleet"]
        # Complete dag: geen nieuw verzoek; vandaag nooit ophalen.
        await e.tennet_prijzen(gister)
        assert await e.tennet_prijzen(calc.vandaag(e.now_ms())) == {}
        assert len(fs.tennet_log) == 1
        p = await e.prijzen_dag(gister)
        assert p[a]["tnI"] and p[a]["vi"]


async def test_oude_dag_blijft_beschikbaar_na_opruimen(server):
    async with aiohttp.ClientSession() as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, tennet=api.TennetClient(s))
        oud = calc.plus_dagen(calc.vandaag(e.now_ms()), -60)
        assert (await e.tennet_status(oud)) == (True, "")
        assert oud not in st["tennet"]  # opgeruimd uit de opslag …
        n = len(fs.tennet_log)
        assert len(await e.prijzen_dag(oud)) > 0  # … maar uit het geheugen, zonder nieuw verzoek
        assert len(fs.tennet_log) == n


async def test_andere_datum_en_onbereikbaar(server):
    async with aiohttp.ClientSession() as s:
        st: dict = {}
        e = Engine(None, None, st, {}, "test", slaap=lambda x: None, tennet=api.TennetClient(s))
        d1 = calc.plus_dagen(calc.vandaag(e.now_ms()), -30)
        fs.tennet_modus["modus"] = "andere_datum"
        ok, reden = await e.tennet_status(d1)
        assert not ok and "in plaats van" in reden
        d2 = calc.plus_dagen(calc.vandaag(e.now_ms()), -31)
        fs.tennet_modus["modus"] = "onbereikbaar"
        ok, reden = await e.tennet_status(d2)
        assert not ok and "HTTP 503" in reden
        d3 = calc.plus_dagen(calc.vandaag(e.now_ms()), -32)
        fs.tennet_modus.update(modus="ok", mist={d3})
        ok, reden = await e.tennet_status(d3)
        assert not ok and reden == "nog niet gepubliceerd"


async def test_historie_verstuurt_maand_alleen_met_complete_tennet(server):
    """Ontbreekt TenneT voor een oudere dag, dan gaat er van die maand niets naar Mijnbatterij."""
    async def slaap(_):
        return None

    class MB:
        async def dagen(self, a, b):
            return {}

    async with aiohttp.ClientSession() as s:
        e = Engine(None, MB(), {}, {"proef": True}, "test", slaap=slaap, tennet=api.TennetClient(s))
        vk = calc.vandaag(e.now_ms())
        ym = calc.plus_dagen(vk[:7] + "-01", -40)[:7]  # ruim afgelopen maand
        dagen, d = [], ym + "-01"
        while d[:7] == ym:
            dagen.append(d); d = calc.plus_dagen(d, 1)
        verstuurd: list[str] = []

        async def maand_bron(m, ttl):
            return {"series": [{"date": d, "status": "complete"} for d in dagen] if m == ym else []}

        async def dag_definitief(dk, rij, force):
            return {"dk": dk, "w": {"totaal": 1.0, "besparing": 0.5, "geladen": 10.0, "ontladen": 9.0}}

        async def verstuur(r, best, rapport):
            verstuurd.append(r["dk"])

        e.maand_bron, e.dag_definitief, e._verstuur_dag = maand_bron, dag_definitief, verstuur
        fs.tennet_modus.update(modus="ok", mist={dagen[10]})
        rapport = await e._historie(ym, lambda: False)
        assert verstuurd == [], verstuurd
        assert any("NIET verstuurd" in r and dagen[10] in r for r in rapport), rapport
        # TenneT compleet: alle dagen gaan mee.
        fs.tennet_modus.update(mist=set())
        e._tn_mem.clear(); e.s["tennet"].clear()
        rapport = await e._historie(ym, lambda: False)
        assert verstuurd == dagen, rapport
