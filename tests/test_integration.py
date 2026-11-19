"""Volledige HA-test: config flow, setup, sensoren, knop, service — tegen nep-servers."""
import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.be_mijnbatterij import api
from custom_components.be_mijnbatterij.const import DOMAIN

from . import fake_servers as fs

GEGEVENS = {"be_username": "luc@example.nl", "be_password": "geheim", "mb_api_key": "MBKEY"}


@pytest.fixture
async def server(socket_enabled, monkeypatch):
    srv = TestServer(fs.maak_app(), host="127.0.0.1")
    await srv.start_server()
    # De testserver draait op een IP-adres; de cookiejar accepteert dat alleen met unsafe=True.
    # In productie (dashboard.balansenergie.nl) is dat niet nodig.
    echte = aiohttp.CookieJar
    monkeypatch.setattr(aiohttp, "CookieJar", lambda **kw: echte(unsafe=True, **kw))
    api.BE_BASE = api.MB_BASE = f"http://127.0.0.1:{srv.port}"
    yield srv
    await srv.close()


async def test_flow_fout_wachtwoord(hass: HomeAssistant, server) -> None:
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**GEGEVENS, "be_password": "fout"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "be_auth"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**GEGEVENS, "mb_api_key": "FOUT"})
    assert r["errors"] == {"base": "mb_auth"}


async def test_volledig(hass: HomeAssistant, server) -> None:
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**GEGEVENS, "proef": True})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    assert "proef" not in r["data"] and r["options"]["proef"] is True
    assert r["title"].startswith("Balans ABC123")
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is config_entries.ConfigEntryState.LOADED

    states = {s.entity_id: s for s in hass.states.async_all() if s.entity_id.startswith(("sensor.be_mijnbatterij", "button.be_mijnbatterij"))}
    print("\n" + "\n".join(f"  {k} = {v.state}" for k, v in sorted(states.items())))
    reg = er.async_get(hass)
    eid = lambda dom, key: reg.async_get_entity_id(dom, DOMAIN, f"ABC123_{key}")
    assert float(hass.states.get(eid("sensor", "dekking")).state) > 99
    assert hass.states.get(eid("sensor", "status")).state == "proefstand"
    # proefstand: alles met test=true, niets opgeslagen
    assert fs.mb_log and all(b.get("test") for _, b in fs.mb_log)
    assert not fs.mb_db

    # proefstand zichtbaar: schakelaar aan + melding onder Reparaties
    issues = ir.async_get(hass)
    assert hass.states.get(eid("switch", "optie_proef")).state == "on"
    assert issues.async_get_issue(DOMAIN, f"proefstand_{entry.entry_id}")

    # proefstand uit via de schakelaar → melding weg → knop → echt versturen + heartbeat
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid("switch", "optie_proef")}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options["proef"] is False and not entry.runtime_data.engine.proef
    assert hass.states.get(eid("switch", "optie_proef")).state == "off"
    assert not issues.async_get_issue(DOMAIN, f"proefstand_{entry.entry_id}")
    # de optiestroom werkt nog steeds en blijft gelijk lopen met de schakelaars
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"versturen": True, "proef": False, "saldeert": True, "afrr": False})
    await hass.async_block_till_done()
    assert hass.states.get(eid("switch", "optie_afrr")).state == "off"
    await hass.services.async_call("button", "press", {"entity_id": eid("button", "nu_versturen")}, blocking=True)
    await hass.async_block_till_done()
    assert fs.mb_db, "geen dagen opgeslagen"
    labels = [fs.calc.lees_label(v["note"]) for v in fs.mb_db.values()]
    assert any(l and l.get("actief") and l.get("bron") == "ha" for l in labels), labels
    print("  opgeslagen dagen:", sorted(fs.mb_db))

    res = await hass.services.async_call(DOMAIN, "historie_herstellen", {"vanaf": fs.DK[:7]}, blocking=True, return_response=True)
    print("  historie:", list(res["rapport"].values())[0][:2])
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_standaard_echt(hass: HomeAssistant, server) -> None:
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], GEGEVENS)
    assert r["options"]["proef"] is False
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"proefstand_{entry.entry_id}")
    assert await hass.config_entries.async_unload(entry.entry_id)
