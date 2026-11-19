"""Nep-BE en nep-Mijnbatterij voor de tests."""
import time
from aiohttp import web
from custom_components.be_mijnbatterij import calc


NU = int(time.time() * 1000)
DK = calc.vandaag(NU); GISTER = calc.plus_dagen(DK, -1); EERGISTER = calc.plus_dagen(DK, -2)
mb_db, mb_log, be_log = {}, [], []
staat = {"sessies": set(), "verloop": False}

def rows_tussen(a, b):
    out, t = [], (a // 10000) * 10000
    while t <= b:
        h = (t // 3600000) % 24
        bo = -6000 if h < 5 else (5000 if 17 <= h < 21 else 0)
        out.append({"t": t, "po": 800 - bo, "bo": bo, "s": 50.0}); t += 10000
    return out

async def login_get(req):
    r = web.Response(text='<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="TOK123"><input name="username"><input name="password" type="password"></form>', content_type="text/html")
    r.set_cookie("csrftoken", "COOKIE123"); return r
async def login_post(req):
    f = await req.post(); be_log.append("login")
    if f.get("csrfmiddlewaretoken") != "TOK123" or req.cookies.get("csrftoken") != "COOKIE123" or not req.headers.get("Referer"):
        return web.Response(status=403, text="CSRF")
    if f.get("username") != "luc@example.nl" or f.get("password") != "geheim":
        return web.Response(text="<form>Onjuist</form>", content_type="text/html")
    sid = f"S{len(staat['sessies'])}"; staat["sessies"].add(sid)
    r = web.HTTPFound("/customer/"); r.set_cookie("sessionid", sid); raise r
def ingelogd(req):
    sid = req.cookies.get("sessionid")
    if staat["verloop"]: staat["verloop"] = False; staat["sessies"].clear()
    if sid not in staat["sessies"]: raise web.HTTPFound("/accounts/login/?next=" + req.path)
async def customer(req):
    ingelogd(req); return web.Response(text='<a href="/customer/ABC123/results">Resultaten</a>', content_type="text/html")
async def history(req):
    ingelogd(req); be_log.append("history")
    assert req.query["installation"] == "ABC123"
    a, b = int(req.query["start"]), int(req.query["end"])
    return web.json_response({"rows": rows_tussen(max(a, NU - 26*3600000), b), "resolutionMs": 10000})
async def prices(req):
    ingelogd(req)
    if "start" in req.query:
        be_log.append("prices-range"); return web.Response(status=400, text="bad")
    start, eind = calc.grenzen(GISTER)[0], calc.grenzen(DK)[1]
    # Zoals BE sinds 17-11-2026: nog maar voor een klein deel van de kwartieren een prijs.
    def waarden(q):
        h = (q // 3600000) % 24
        if h < 2:
            return {"onbalans_import": 0.1 + (q // 900000 % 4) * 0.05, "onbalans_export": 0.08 + (q // 900000 % 4) * 0.05}
        return {}
    return web.json_response({"prices": [{"start": q, "end": q + 900000, "provisional": True, "values": waarden(q)} for q in range(start, min(eind, NU), 900000)],
                              "dayAheadPlans": [], "dayAheadUnavailableWindows": []})
async def tenergy(req):
    """Nep-Tenergy: ~30 uur minuutwaarden in de grafiekdata, zonder activatietabel."""
    import json
    nu = int(time.time() * 1000)
    t0 = ((nu - 30 * 3600000) // 60000) * 60000
    take = [{"Key": f"/Date({t})/", "Value": 150.0 + (t // 900000 % 3) * 10} for t in range(t0, nu, 60000)]
    feed = [{"Key": f"/Date({t})/", "Value": 120.0 + (t // 900000 % 3) * 10} for t in range(t0, nu, 60000)]
    data = {"ChartSeries": [{"Id": "PRICES_TAKE", "Data": take}, {"Id": "PRICES_FEED", "Data": feed}]}
    return web.Response(text=f"<script>data = {json.dumps(data)}; serialChart.updateChart(data, 1);</script>", content_type="text/html")
tennet_log = []
async def tennet_pagina(req):
    """Nep-TenneT-publicatie (Oracle APEX): pagina met sessievelden en een sessiecookie."""
    tennet_log.append(("pagina", req.query.get("p", "")))
    html = ('<input type="hidden" name="p_instance" value="4960515031577" id="pInstance" />'
            '<input type="hidden" value="40567796882410854816" id="pSalt" />'
            '<input type="hidden" id="pPageItemsProtected" value="UDkwMDMw&#x2F;I20G-IQ" />'
            '<script>apex.widget.jetChart.init("R84965069442921348","","400",(function( options ){ return options; }),false,true,null,'
            '"UkVHSU9OIFRZUEV-fjg0OTY1\\u002FhbWo4jjG");</script>')
    r = web.Response(text=html, content_type="text/html")
    r.set_cookie("ORA_WWV_APP_100", "SESSIE123")
    return r
async def tennet_ajax(req):
    import json
    form = await req.post()
    tennet_log.append(("ajax", req.headers.get("Cookie", "")))
    if "ORA_WWV_APP_100=SESSIE123" not in req.headers.get("Cookie", "") or form.get("p_instance") != "4960515031577" \
            or form.get("p_request") != "PLUGIN=UkVHSU9OIFRZUEV-fjg0OTY1/hbWo4jjG":
        return web.Response(text="<!DOCTYPE html><html>sessie verlopen</html>", content_type="text/html")
    pj = json.loads(form["p_json"])
    d = pj["pageItems"]["itemsToSubmit"][0]["v"]
    dk = f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
    items = {"Shortage": [], "Surplus": []}
    if dk < calc.vandaag(int(time.time() * 1000)):
        a, b = calc.grenzen(dk)
        for i in range((b - a) // 900000):
            naam = f" {i + 1:03d}-{(i // 4) % 24:02d}:{(i % 4) * 15:02d}"
            items["Shortage"].append({"id": str(i), "value": 100.0 + i, "name": naam})
            items["Surplus"].append({"id": str(200 + i), "value": -50.0 + i if i != 40 else 3500.0, "name": naam})
    return web.json_response({"series": [{"name": k, "items": v} for k, v in items.items()]})
async def results(req):
    ingelogd(req); be_log.append("results-" + req.query["interval"])
    per = req.query["period"]
    if req.query["interval"] == "month":
        dagen = [calc.plus_dagen(DK, -i) for i in range(1, 11) if calc.plus_dagen(DK, -i)[:7] == per]
        return web.json_response({"interval": "month", "period": per, "timezone": "Europe/Amsterdam", "pricing": {"include_vat": False, "include_delivery_cost": False},
            "series": [{"date": d, "status": "complete" if d < GISTER else "provisional", "battery_charged_kwh": 30.0, "battery_discharged_kwh": 20.0,
                        "avg_buy_price_eur_per_kwh": 0.11, "avg_sell_price_eur_per_kwh": 0.09, "balance_energie_eur": 0.4} for d in dagen]})
    s, e = calc.grenzen(per); iv = []
    for q in range(s, e, 900000):
        h = (q - s) // 3600000
        lad = 2.0 if h < 5 and (q - s) % 3600000 < 2700000 else 0.0
        ont = 1.25 if 17 <= h < 21 else 0.0
        iv.append({"timestamp": calc.iso_ams(q), "grid_import_kwh": lad + 0.2, "grid_export_kwh": ont * 0.6, "battery_charged_kwh": lad,
                   "battery_discharged_kwh": ont, "effective_import_price_eur_per_kwh": 0.12, "effective_export_price_eur_per_kwh": 0.3})
    return web.json_response({"interval": "day", "period": per, "pricing": {"include_vat": False, "include_delivery_cost": False}, "day_detail": {"intervals": iv}})

def mb_auth(req):
    if req.headers.get("Authorization") != "Bearer MBKEY": raise web.HTTPUnauthorized()
async def mb_me(req): mb_auth(req); return web.json_response({"username": "paq"})
async def mb_live(req):
    mb_auth(req); b = await req.json(); mb_log.append(("live", b)); return web.json_response({})
async def mb_daily_post(req):
    mb_auth(req); b = await req.json(); mb_log.append(("daily", b))
    if not b.get("test"): mb_db[b["date"]] = {"date": b["date"], "type": "api", "batteryResult": float(b["batteryResult"]), "note": b["note"]}
    return web.json_response({})
async def mb_daily_get(req):
    mb_auth(req); a, b = req.query["from"], req.query["to"]
    return web.json_response({"results": [v for k, v in sorted(mb_db.items(), reverse=True) if a <= k <= b]})



def maak_app():
    app = web.Application()
    app.add_routes([web.get("/accounts/login/", login_get), web.post("/accounts/login/", login_post), web.get("/customer/", customer),
                    web.get("/customer/current-history/api/", history), web.get("/customer/current-prices/api/", prices),
                    web.get("/customer/ABC123/results-v2-api/", results), web.get("/tenergy", tenergy), web.get("/ords/f", tennet_pagina), web.post("/ords/wwv_flow.ajax", tennet_ajax),
                    web.get("/api/me", mb_me), web.post("/api/live", mb_live), web.post("/api/results/daily", mb_daily_post), web.get("/api/results/daily", mb_daily_get)])
    return app
