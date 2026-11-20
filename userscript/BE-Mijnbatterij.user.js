// ==UserScript==
// @name         BE Mijnbatterij
// @namespace    paq.balansenergie
// @version      1.4.1
// @description  Deelt accuresultaten van het Balans Energie-dashboard met mijnbatterij.nl, gesplitst in handel en besparing. Los van Balansenergie All-in; eigen pagina in het menu.
// @homepageURL  https://github.com/paqpaqpaq/be_mijnbatterij
// @updateURL    https://raw.githubusercontent.com/paqpaqpaq/be_mijnbatterij/main/userscript/BE-Mijnbatterij.user.js
// @downloadURL  https://raw.githubusercontent.com/paqpaqpaq/be_mijnbatterij/main/userscript/BE-Mijnbatterij.user.js
// @supportURL   https://github.com/paqpaqpaq/be_mijnbatterij/issues
// @match        *://dashboard.balansenergie.nl/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM.getValue
// @grant        GM.setValue
// @connect      api.mijnbatterij.nl
// @connect      publications.tennet.eu
// ==/UserScript==

/* ─── Brug naar api.mijnbatterij.nl (userscriptlaag) ─────────────────────
 * Alleen deze laag heeft GM-rechten. De API-sleutel staat in de
 * userscript-opslag en komt nooit in de pagina. Pagina en brug praten via
 * twee JSON-nodes en DOM-events; verzoeken worden in de pagina geserialiseerd.
 */
(function () {
  'use strict';
  if (document.getElementById('bemb-in')) return;
  var API = 'https://api.mijnbatterij.nl';
  var KEY = 'bemb_key';
  var inNode = document.createElement('script');
  inNode.type = 'application/json'; inNode.id = 'bemb-in';
  document.documentElement.appendChild(inNode);

  function gmx() {
    if (typeof GM_xmlhttpRequest === 'function') return GM_xmlhttpRequest;
    if (typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function') return GM.xmlHttpRequest.bind(GM);
    return null;
  }
  async function getKey() {
    try { if (typeof GM_getValue === 'function') return String(GM_getValue(KEY, '') || ''); } catch (e) {}
    try { if (typeof GM !== 'undefined' && GM && GM.getValue) return String((await GM.getValue(KEY, '')) || ''); } catch (e) {}
    return '';
  }
  async function setKey(v) {
    try { if (typeof GM_setValue === 'function') { GM_setValue(KEY, v); return; } } catch (e) {}
    if (typeof GM !== 'undefined' && GM && GM.setValue) { await GM.setValue(KEY, v); return; }
    throw new Error('Geen userscript-opslag beschikbaar');
  }
  /* TenneT-publicatie van de settlementprijzen (openbaar, zonder sleutel en zonder cookie): de tabelpagina
   * achter tennet.eu/nl-en/settlement-prices-table (Oracle APEX, pagina 90035). Eén GET per dag, met de
   * datum in de URL en alle kwartieren op één pagina. Alleen lezen. */
  var TENNET = 'https://publications.tennet.eu/ords/', TENNET_REGIO = '93369768093509395';
  function gmReq(o) {
    return new Promise(function (res) {
      var fn = gmx(); if (!fn) { res({ fout: 'GM_xmlhttpRequest ontbreekt' }); return; }
      fn(Object.assign({ timeout: 25000, anonymous: true }, o, {
        onload: function (r) { res({ status: r.status, text: String(r.responseText || '') }); },
        onerror: function (r) { res({ fout: 'Netwerkfout richting TenneT' + (r && r.error ? ' (' + String(r.error).slice(0, 120) + ')' : '') }); },
        ontimeout: function () { res({ fout: 'Timeout richting TenneT' }); }
      }));
    });
  }
  function tennetUrl(datum, regio) {
    return TENNET + 'f?p=100:90035:0:' + (regio ? 'pg_R_' + regio : '') + ':NO::P90035_PTU_DATE_LOC:' + datum +
      (regio ? '&pg_min_row=1&pg_max_rows=200&pg_rows_fetched=200' : '') + '&p_lang=en';
  }
  // Tabel lezen: {datum, rijen: {ISP: [tekort, overschot, regeltoestand]}} (€/MWh), of null bij een onherkenbare pagina.
  function tennetTabel(html) {
    var d = new DOMParser().parseFromString(html, 'text/html'), dt = d.getElementById('P90035_PTU_DATE_LOC');
    var kop = Array.prototype.map.call(d.querySelectorAll('table th'), function (t) { return t.textContent.trim().toLowerCase(); });
    var iT = kop.findIndex(function (k) { return k.indexOf('shortage') === 0; }), iO = kop.findIndex(function (k) { return k.indexOf('surplus') === 0; }), iR = kop.findIndex(function (k) { return k.indexOf('regulation state') === 0; });
    // Pagina zonder tabel (dag nog niet gepubliceerd): geen rijen. Echt onherkenbaar alleen als het niet eens deze pagina is.
    if (iT < 0 && iO < 0 && !d.querySelector('table th') && /id="pFlowStepId"[^>]*value="90035"|value="90035"[^>]*id="pFlowStepId"/.test(html)) return { datum: null, rijen: {}, leeg: true };
    if (!dt || iT < 0 || iO < 0) return null;
    var getalT = function (x) { x = String(x || '').replace(/,/g, '').trim(); if (!/^-?\d*\.?\d+$/.test(x)) return null; var v = parseFloat(x); return isFinite(v) ? v : null; };
    var rijen = {};
    Array.prototype.forEach.call(d.querySelectorAll('table tbody tr'), function (tr) {
      var c = Array.prototype.map.call(tr.cells, function (x) { return x.textContent.trim(); });
      if (!/^\d+$/.test(c[0] || '')) return;
      rijen[+c[0]] = [getalT(c[iT]), getalT(c[iO]), iR >= 0 ? getalT(c[iR]) : null];
    });
    var pag = /apex\.widget\.report\.paginate\('(\d+)'/.exec(html);
    return { datum: String(dt.value || dt.getAttribute('value') || ''), rijen: rijen, regio: pag ? pag[1] : null };
  }
  async function tennetDag(datum) {
    if (!/^\d{2}-\d{2}-\d{4}$/.test(datum)) return { ok: false, error: 'Ongeldige datum' };
    var regio = TENNET_REGIO, t = null;
    for (var poging = 0; poging < 2; poging++) {
      var p = await gmReq({ method: 'GET', url: tennetUrl(datum, regio), headers: { 'Accept': 'text/html' } });
      if (p.fout) return { ok: false, error: p.fout };
      if (p.status !== 200) return { ok: false, error: 'TenneT-pagina HTTP ' + p.status };
      t = tennetTabel(p.text);
      if (!t) return { ok: false, error: 'TenneT-tabel onherkenbaar (opbouw gewijzigd?)' };
      // Andere regio-id (pagina aangepast): de id staat in de bladerknop; dan één keer opnieuw.
      if (t.regio && t.regio !== regio && Object.keys(t.rijen).length < 92) { regio = t.regio; continue; }
      break;
    }
    if (t.leeg) return { ok: true, rijen: {} };
    // Nooit de prijzen van een andere dag gebruiken: TenneT valt bij een onbekende datum terug op gisteren.
    if (t.datum !== datum) return { ok: false, error: 'TenneT gaf ' + (t.datum || 'geen datum') + ' in plaats van ' + datum };
    return { ok: true, rijen: t.rijen };
  }
  var GET_OK = /^\/api\/me$|^\/api\/results\/(?:daily|monthly)\?from=\d{4}-\d{2}(?:-\d{2})?&to=\d{4}-\d{2}(?:-\d{2})?$/;
  var POST_OK = /^\/api\/(?:live|results\/daily|results\/monthly)$/;

  document.addEventListener('bemb-send', async function () {
    var out = document.getElementById('bemb-out'), v;
    try { v = JSON.parse(out && out.textContent || '{}'); } catch (e) { return; }
    // Verzoek direct uit de DOM halen: de sleutel mag daar niet blijven staan.
    if (out) out.textContent = '';
    if (!v || !v.id) return;
    function answer(a) { a.id = v.id; inNode.textContent = JSON.stringify(a); document.dispatchEvent(new Event('bemb-answer')); }
    try {
      if (v.op === 'key-status') { answer({ ok: true, hasKey: !!(await getKey()).trim() }); return; }
      if (v.op === 'key-set') {
        var nieuw = String(v.value || '').trim();
        if (nieuw && !/^[\x21-\x7e]{8,512}$/.test(nieuw)) { answer({ ok: false, error: 'Sleutel bevat ongeldige tekens' }); return; }
        await setKey(nieuw); answer({ ok: true, hasKey: !!nieuw }); return;
      }
      if (v.op === 'tennet') { answer(await tennetDag(String(v.datum || ''))); return; }
      if (v.op !== 'api') return;
      var method = v.method === 'POST' ? 'POST' : 'GET';
      if (method === 'GET' ? !GET_OK.test(v.path) || v.body : !POST_OK.test(v.path) || !v.body) { answer({ ok: false, error: 'Verzoek niet toegestaan' }); return; }
      var key = (await getKey()).trim();
      if (!key) { answer({ ok: false, error: 'Geen API-sleutel ingesteld' }); return; }
      var fn = gmx();
      if (!fn) { answer({ ok: false, error: 'GM_xmlhttpRequest ontbreekt' }); return; }
      // Netwerkfouten (verbinding weggevallen, extensie even niet bereikbaar) één keer opnieuw proberen.
      var poging = 0;
      var stuur = function () {
        poging++;
        fn({
          method: method, url: API + v.path, timeout: 25000,
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': 'Bearer ' + key, 'User-Agent': 'BE-Mijnbatterij/1.4.0' },
          data: v.body ? JSON.stringify(v.body) : undefined,
          onload: function (r) {
            var ok = r.status >= 200 && r.status < 300, text = String(r.responseText || '');
            if (!r.status && poging < 2) { setTimeout(stuur, 3000); return; }
            answer({ ok: ok, status: r.status, text: text.slice(0, 2000000), error: ok ? '' : (r.status ? 'HTTP ' + r.status + ' — ' + text.slice(0, 300) : 'Geen antwoord van mijnbatterij.nl') });
          },
          onerror: function (r) {
            if (poging < 2) { setTimeout(stuur, 3000); return; }
            var d = r && (r.error || r.statusText || (r.status ? 'status ' + r.status : '')) || '';
            answer({ ok: false, error: 'Netwerkfout richting mijnbatterij.nl' + (d ? ' (' + String(d).slice(0, 120) + ')' : '') });
          },
          ontimeout: function () { answer({ ok: false, error: 'Timeout richting mijnbatterij.nl' }); }
        });
      };
      stuur();
    } catch (e) { answer({ ok: false, error: e && e.message || String(e) }); }
  });
})();

/* ─── Paginadeel ─────────────────────────────────────────────────────────── */
function bembPage() {
  'use strict';
  if (window.__bembLoaded) return;
  window.__bembLoaded = true;

  var TENNET_PAGINA = 'https://publications.tennet.eu/ords/';
  var VERSIE = '1.4.1', Q = 900000, HASH = '#be-mijnbatterij', TZ = 'Europe/Amsterdam';
  // Marge (procentpunt): twee verzenders met vrijwel volledige data mogen elkaar vervangen;
  // aanvullen naast HA gebeurt pas bij +1 pp, zodat ze niet pingpongen.
  var TOLERANTIE = 0.5;
  var BTW = 1.21, OPSLAG = 0.02;
  // Energiebelasting excl. btw, schijf 1. Saldering eindigt op 1-1-2027.
  var EB_EX = { 2025: 0.10154, 2026: 0.09161, 2027: 0.08802 };
  var RAUW_DAGEN = 10, PRIJS_DAGEN = 16, DAG_VENSTER = 10;
  var MIN_DEKKING_VOORLOPIG = 0.99; // voorlopige dag alleen bij (vrijwel) volledige metingen
  var coulombCache = { at: 0, id: '', rows: [] }, ARCHIEF_GAT = 300000;
  var MAX_KAAL = 1.5, MIN_KWH = 0.05; // grenzen voor een bruikbare effectieve kwartierprijs (kosten/kWh, €/kWh)
  // Settlementprijzen (BE market_breakdown, TenneT, vastgesteld archief) zijn echte marktprijzen en
  // kunnen ver boven € 1,50 liggen (15-06-2026 09:45: € 3,50/kWh). Alleen een ruime plausibiliteitsgrens.
  var MAX_SETTLE = 100; // €/kWh: ruim boven elk marktplafond, alleen tegen onzinwaarden

  /* ── Tijd ── */
  var fmtDag = new Intl.DateTimeFormat('sv-SE', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  var fmtParts = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  var fmtKlok = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
  var fmtDatum = new Intl.DateTimeFormat('nl-NL', { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short' });
  function dagVan(t) { return fmtDag.format(new Date(t)); }
  function vandaag() { return dagVan(Date.now()); }
  function klok(t) { return fmtKlok.format(new Date(t)); }
  function plusDagen(dk, n) { var d = new Date(Date.parse(dk + 'T12:00:00Z') + n * 86400000); return d.toISOString().slice(0, 10); }
  var grensCache = {};
  function grenzen(dk) {
    if (grensCache[dk]) return grensCache[dk];
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dk)) return null;
    var t = Date.parse(dk + 'T00:00:00Z') - 3 * 3600000, limiet = t + 36 * 3600000;
    while (t < limiet && dagVan(t) !== dk) t += Q;
    if (t >= limiet) return null;
    var start = t; while (dagVan(t) === dk) t += Q;
    return (grensCache[dk] = { start: start, eind: t });
  }
  function isoAms(t) {
    var p = {}; fmtParts.formatToParts(new Date(t)).forEach(function (x) { p[x.type] = x.value; });
    var off = Math.round((Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(t / 1000) * 1000) / 60000);
    var a = Math.abs(off);
    return p.year + '-' + p.month + '-' + p.day + 'T' + p.hour + ':' + p.minute + ':' + p.second + (off < 0 ? '-' : '+') +
      String(Math.floor(a / 60)).padStart(2, '0') + ':' + String(a % 60).padStart(2, '0');
  }
  function maanden(van, tot) { var out = [], y = +van.slice(0, 4), m = +van.slice(5, 7); for (;;) { var k = y + '-' + String(m).padStart(2, '0'); if (k > tot) break; out.push(k); if (++m > 12) { m = 1; y++; } } return out; }
  function dagenInMaand(ym) { return new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7), 0)).getUTCDate(); }
  function getal(n) { return typeof n === 'number' && isFinite(n); }
  function wacht(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  // Pauze vóór elk BE-verzoek voor een dag/maand. Tijdens historie ruimer: BE's limiet is niet
  // bekend en All-in (en eventueel Home Assistant) gebruikt hetzelfde account.
  var BE_PAUZE = 1250, HISTORIE_PAUZE = 4000;

  /* ── Rekenen (los van DOM, testbaar) ── */
  // Verdeling per meetsegment, zoals Mijnbatterij het voorschrijft:
  // huisverbruik krijgt zonnestroom voorrang; de accu laadt eerst het
  // zonne-overschot (tegen terugleverprijs) en daarna uit het net (afnameprijs).
  // po > 0 is afname, bo > 0 is ontladen. rest = huis − zon (zonder accu).
  function verdeel(po, bo) {
    var ont = Math.max(bo, 0), lad = Math.max(-bo, 0), rest = po + bo;
    var ontHuis = Math.min(ont, Math.max(rest, 0)), ladPV = Math.min(lad, Math.max(-rest, 0));
    return { ontHuis: ontHuis, ontNet: ont - ontHuis, ladPV: ladPV, ladNet: lad - ladPV };
  }
  /* ── Omvormerrendement ──
   * BE's accuvermogen (bo) is DC. Voor de waardering telt wat er aan de AC-kant
   * het huis/net in of uit gaat: ontladen AC = DC × η_uit, laden AC = DC / η_in.
   * η wordt per installatie gemeten op momenten zonder PV (p en g ≈ 0): dan is
   * de huisbalans exact (accu AC = huis − net). Standaard = gemeten op 19-11-2026. */
  var ETA_STANDAARD = { uit: [0.945, 0.945, 0.881], in: [0.957, 0.957, 0.957] };
  var ETA = ETA_STANDAARD;
  function etaKlasse(w, laden) { var a = Math.abs(w); return laden ? (a < 3000 ? 0 : a < 7000 ? 1 : 2) : (a < 2000 ? 0 : a < 5000 ? 1 : 2); }
  function naarAC(bo, e) {
    e = e || ETA;
    if (bo > 0) return bo * e.uit[etaKlasse(bo, false)];
    if (bo < 0) return bo / e.in[etaKlasse(bo, true)];
    return 0;
  }
  // Energiegewogen per vermogensklasse; een klasse telt pas mee vanaf 50 metingen,
  // anders het richtingsgemiddelde (vanaf 100), anders de standaard.
  function kalibreer(rows) {
    var s = { uit: [[0, 0, 0], [0, 0, 0], [0, 0, 0]], in: [[0, 0, 0], [0, 0, 0], [0, 0, 0]] };
    (rows || []).forEach(function (r) {
      if (!r || ![r.po, r.bo, r.l, r.p, r.g].every(getal) || r.p >= 15 || r.g >= 15 || Math.abs(r.bo) <= 800) return;
      var laden = r.bo < 0, k = etaKlasse(r.bo, laden), cel = s[laden ? 'in' : 'uit'][k];
      if (laden) { cel[0] += -r.bo; cel[1] += r.po - r.l; } else { cel[0] += r.l - r.po; cel[1] += r.bo; }
      cel[2]++;
    });
    function richting(naam) {
      var t = s[naam].reduce(function (a, c) { return [a[0] + c[0], a[1] + c[1], a[2] + c[2]]; }, [0, 0, 0]);
      var totaal = t[2] >= 100 && t[1] > 0 ? t[0] / t[1] : null;
      return s[naam].map(function (c, i) {
        var v = c[2] >= 50 && c[1] > 0 ? c[0] / c[1] : totaal;
        return getal(v) && v >= 0.8 && v <= 0.99 ? Math.round(v * 1000) / 1000 : ETA_STANDAARD[naam][i];
      });
    }
    return { uit: richting('uit'), in: richting('in'), n: { uit: s.uit.map(function (c) { return c[2]; }), in: s.in.map(function (c) { return c[2]; }) } };
  }
  function etaTekst(e) { return 'Omvormerrendement uit ' + e.uit.map(function (x) { return x.toFixed(2); }).join('/') + ', in ' + e.in.map(function (x) { return x.toFixed(2); }).join('/'); }
  function gatGrens(r) { return Math.max(120000, Math.min(Math.round((r || 0) * 1.5), 600000)); }
  // Tijdgewogen integratie naar kwartieren (kWh). Gaten worden nooit opgevuld.
  function naarKwartieren(rows, e) {
    var s = (rows || []).filter(function (r) { return r && getal(r.t) && getal(r.po); }).sort(function (a, b) { return a.t - b.t; });
    var kw = {};
    for (var i = 0; i < s.length - 1; i++) {
      var r = s[i], n = s[i + 1], dt = n.t - r.t;
      if (dt <= 0 || dt > gatGrens(r.r)) continue;
      for (var pos = r.t; pos < n.t;) {
        var q = Math.floor(pos / Q) * Q, stop = Math.min(n.t, q + Q), uur = (stop - pos) / 3600000, sec = (stop - pos) / 1000;
        var c = kw[q] || (kw[q] = { imp: 0, exp: 0, dek: 0, ontHuis: 0, ontNet: 0, ladNet: 0, ladPV: 0, accuSec: 0, geladen: 0, ontladen: 0, pv: 0, pvSec: 0, load: 0, loadSec: 0 });
        if (r.po > 0) c.imp += r.po * uur / 1000; else c.exp += -r.po * uur / 1000;
        c.dek += sec;
        // Zon = PV op de Balans-omvormer (p) + PV bij het huis (g); huis = l. Gecontroleerd: l ≈ po + bo(AC) + p + g.
        if (getal(r.p) || getal(r.g)) { c.pv += ((getal(r.p) ? r.p : 0) + (getal(r.g) ? r.g : 0)) * uur / 1000; c.pvSec += sec; }
        if (getal(r.l)) { c.load += r.l * uur / 1000; c.loadSec += sec; }
        if (getal(r.bo)) {
          // Verdeling en waardering in AC; geladen/ontladen blijven de gemeten DC-kWh.
          var v = verdeel(r.po, naarAC(r.bo, e));
          c.ontHuis += v.ontHuis * uur / 1000; c.ontNet += v.ontNet * uur / 1000;
          c.ladNet += v.ladNet * uur / 1000; c.ladPV += v.ladPV * uur / 1000;
          c.geladen += Math.max(-r.bo, 0) * uur / 1000; c.ontladen += Math.max(r.bo, 0) * uur / 1000;
          c.accuSec += sec;
        }
        if (getal(n.s) && n.s >= 0 && n.s <= 100 && stop === n.t) c.soc = n.s;
        pos = stop;
      }
    }
    return kw;
  }
  function allin(kaal, isExport, t, c) {
    if (!getal(kaal)) return null;
    var jaar = +dagVan(t).slice(0, 4), ebEx = EB_EX[jaar];
    if (!getal(ebEx)) return null;
    var eb = ebEx * BTW;
    return isExport ? kaal * BTW - OPSLAG + (c.saldeert && jaar < 2027 ? eb : 0) : kaal * BTW + OPSLAG + eb;
  }
  // Waardering per dag. Besparing en handel volgen de bestemming van de
  // ontladen kWh; laadkosten worden naar rato van die kWh verdeeld.
  function waardeer(kw, c, afrrEur) {
    var t = { ontHuis: 0, ontNet: 0, ladNet: 0, ladPV: 0, huis: 0, net: 0, kNet: 0, kPV: 0, imp: 0, exp: 0,
      geladen: 0, ontladen: 0, accuSec: 0, prijsSec: 0, vastSec: 0, onzeker: 0, zonderPrijs: 0, voorlopigePrijs: 0, kwartieren: 0,
      pv: 0, pvExport: 0, pvOnbekend: 0, impKost: 0, impMetPrijs: 0, expOpbr: 0, expMetPrijs: 0 };
    Object.keys(kw).forEach(function (k) {
      var x = kw[k], q = +k; t.kwartieren++;
      t.imp += x.imp || 0; t.exp += x.exp || 0;
      // Zon: opgewekt, en wat daarvan naar het net ging (teruglevering die niet uit de accu kwam).
      var pvK = getal(x.pv) && (!getal(x.pvSec) || x.pvSec >= 0.9 * (x.dek || 900)) ? x.pv : null;
      if (pvK === null) t.pvOnbekend++;
      else { t.pv += pvK; t.pvExport += Math.min(pvK, Math.max((x.exp || 0) - (x.ontNet || 0), 0)); }
      // Gemiddelde all-in in- en verkoopprijs van het huishouden (gewogen naar afname/teruglevering).
      var piH = getal(x.pIn) ? allin(x.pIn, false, q, c) : null, peH = getal(x.pUit) ? allin(x.pUit, true, q, c) : null;
      if (getal(piH) && x.imp > 0) { t.impKost += piH * x.imp; t.impMetPrijs += x.imp; }
      if (getal(peH) && x.exp > 0) { t.expOpbr += peH * x.exp; t.expMetPrijs += x.exp; }
      if (!(x.accuSec > 0)) return;
      var sec = Math.min(900, x.accuSec); t.accuSec += sec;
      t.geladen += getal(x.geladen) ? x.geladen : x.ladNet + x.ladPV;
      t.ontladen += getal(x.ontladen) ? x.ontladen : x.ontHuis + x.ontNet;
      if (x.ontHuis + x.ontNet + x.ladNet + x.ladPV < 1e-9) { t.prijsSec += sec; t.vastSec += sec; return; }
      var pi = allin(x.pIn, false, q, c), pe = allin(x.pUit, true, q, c);
      if (((x.ontHuis > 0 || x.ladNet > 0) && !getal(pi)) || ((x.ontNet > 0 || x.ladPV > 0) && !getal(pe))) { t.zonderPrijs++; return; }
      t.prijsSec += sec; if (x.prijsVoorlopig) t.voorlopigePrijs++;
      // Alleen vastgestelde prijzen tellen voor de nauwkeurigheid (afstemming plugin ↔ HA).
      if (((x.ontHuis > 0 || x.ladNet > 0) && !x.pInVast) || ((x.ontNet > 0 || x.ladPV > 0) && !x.pUitVast)) t.onzeker++; else t.vastSec += sec;
      t.ontHuis += x.ontHuis; t.ontNet += x.ontNet; t.ladNet += x.ladNet; t.ladPV += x.ladPV;
      t.huis += x.ontHuis * (pi || 0); t.net += x.ontNet * (pe || 0);
      t.kNet += x.ladNet * (pi || 0); t.kPV += x.ladPV * (pe || 0);
      // Alleen tellen als de geschatte prijs in dit kwartier ook echt nodig is.
      if ((x.prijsGeschatIn && (x.ontHuis > 0 || x.ladNet > 0)) || (x.prijsGeschatUit && (x.ontNet > 0 || x.ladPV > 0))) t.geschattePrijs = (t.geschattePrijs || 0) + 1;
      var bijdrage = x.ontHuis * (pi || 0) + x.ontNet * (pe || 0) - x.ladNet * (pi || 0) - x.ladPV * (pe || 0);
      (t.top = t.top || []).push({ q: q, eur: bijdrage, pIn: pi, pUit: pe, ontHuis: x.ontHuis, ontNet: x.ontNet, ladNet: x.ladNet, ladPV: x.ladPV, bron: x.prijsBron || '' });
    });
    if (t.top) t.top = t.top.sort(function (a, b) { return Math.abs(b.eur) - Math.abs(a.eur); }).slice(0, 6);
    var ontl = t.ontHuis + t.ontNet, kosten = t.kNet + t.kPV;
    if (ontl > 1e-9) {
      var aandeel = t.ontHuis / ontl;
      t.besparing = t.huis - aandeel * kosten;
      t.handel = t.net - (1 - aandeel) * kosten;
    } else { t.besparing = -t.kPV; t.handel = -t.kNet; }
    t.afrr = getal(afrrEur) ? afrrEur : 0;
    t.handel += t.afrr;
    t.totaal = t.besparing + t.handel;
    return t;
  }
  // Complete BE-kwartieren: bruto kWh van BE, verdeling uit de eigen cache
  // als die het kwartier goed dekt, anders uit de netto accubeweging.
  // Fysieke grenzen per kwartier (BE rondt accu/huis/zon af op 0,1 kWh): de accu kan niet meer
  // naar het net leveren dan er werd teruggeleverd, en niet meer uit zon laden dan er opgewekt werd.
  function fysiek(n, v) {
    var ont = n.ontHuis + n.ontNet, lad = n.ladNet + n.ladPV;
    n.ontNet = Math.min(n.ontNet, Math.max(v.exp, 0)); n.ontHuis = ont - n.ontNet;
    if (getal(v.pv)) { n.ladPV = Math.min(n.ladPV, Math.max(v.pv, 0)); n.ladNet = lad - n.ladPV; }
  }
  function verdeelDefinitief(bron, cache, e) {
    var uit = {}, uitCache = 0, geschat = 0, geschatActief = 0, aangevuld = 0, ontbrekend = 0;
    Object.keys(bron).forEach(function (q) {
      var v = bron[q], c = cache && cache[q];
      // BE-kwartier zonder accuwaarden: aanvullen uit eigen metingen, anders als ongemeten tellen.
      if (v.leeg) {
        var m = { imp: v.imp, exp: v.exp, accuSec: 0, ladNet: 0, ladPV: 0, ontHuis: 0, ontNet: 0, geladen: 0, ontladen: 0, pv: v.pv, load: v.load };
        if (c && c.accuSec >= 810) {
          ['ladNet', 'ladPV', 'ontHuis', 'ontNet'].forEach(function (f) { m[f] = c[f]; });
          m.geladen = getal(c.geladen) ? c.geladen : 0; m.ontladen = getal(c.ontladen) ? c.ontladen : 0; m.accuSec = 900; m.soc = c.soc; aangevuld++;
        } else ontbrekend++;
        uit[q] = m; return;
      }
      var cl = c ? c.ladNet + c.ladPV : 0, co = c ? c.ontHuis + c.ontNet : 0;
      // BE-kWh zijn DC; omrekenen naar AC met het rendement bij het kwartiergemiddelde vermogen.
      var ladAC = -naarAC(-v.laden * 4000, e) / 4000, ontAC = naarAC(v.ontladen * 4000, e) / 4000;
      var n = { imp: v.imp, exp: v.exp, accuSec: 900, geladen: v.laden, ontladen: v.ontladen, soc: c && c.soc, pv: v.pv, load: v.load };
      if (c && c.accuSec >= 810 && (cl > 0 || v.laden === 0) && (co > 0 || v.ontladen === 0)) {
        n.ladPV = cl ? ladAC * c.ladPV / cl : 0; n.ladNet = ladAC - n.ladPV;
        n.ontHuis = co ? ontAC * c.ontHuis / co : 0; n.ontNet = ontAC - n.ontHuis; uitCache++;
        if (!getal(n.pv) && c.pvSec >= 810) n.pv = c.pv;
      } else if (v.laden > 0 && v.ontladen > 0 && getal(v.load) && getal(v.pv)) {
        // Laden én ontladen in één kwartier: volgorde onbekend. Verdeel met BE's huisverbruik en zon:
        // ontladen dekt eerst de huisvraag na zon, laden komt eerst uit het zonne-overschot.
        var vraag = Math.max(v.load - v.pv, 0), over = Math.max(v.pv - v.load, 0);
        n.ontHuis = Math.min(ontAC, vraag); n.ontNet = ontAC - n.ontHuis;
        n.ladPV = Math.min(ladAC, over); n.ladNet = ladAC - n.ladPV; geschat++; geschatActief++;
        fysiek(n, v);
      } else {
        // Eén richting in het kwartier: uit de netstromen volgt de verdeling exact (op kwartierniveau).
        var netto = ontAC - ladAC; if (Math.abs(netto) < 1e-9) netto = 0;
        var ont = Math.max(netto, 0), lad = Math.max(-netto, 0), rest = v.imp - v.exp + netto;
        n.ladPV = Math.min(lad, Math.max(-rest, 0)); n.ladNet = lad - n.ladPV;
        n.ontHuis = Math.min(ont, Math.max(rest, 0)); n.ontNet = ont - n.ontHuis; geschat++;
        fysiek(n, v);
        // Onzeker alleen als er in hetzelfde kwartier ook laden+ontladen of afname+teruglevering was.
        if ((v.laden > 0 && v.ontladen > 0) || ((v.laden > 0 || v.ontladen > 0) && v.imp > 0.02 && v.exp > 0.02)) geschatActief++;
      }
      uit[q] = n;
    });
    return { kw: uit, n: Object.keys(bron).length, uitCache: uitCache, geschat: geschat, geschatActief: geschatActief, aangevuld: aangevuld, ontbrekend: ontbrekend };
  }
  // Nauwkeurigheid van een definitieve dag (0–1): aandeel kwartieren met verdeling uit eigen
  // metingen en een echte prijs. Bepaalt welke verzender een BE-dag mag vervangen.
  function kwaliteit(d, w) {
    var n = d.n || 0; if (!n) return 0;
    var slecht = d.geschatActief + d.ontbrekend + (w.geschattePrijs || 0) + (w.zonderPrijs || 0) + (w.onzeker || 0);
    return Math.max(0, Math.min(1, (n - slecht) / n));
  }
  function prijsKeuze(p, ctx, events, disc) {
    var state = ctx.authoritative ? (ctx.planned.has(p.start) ? 'planned' : ctx.onbekend(p.start) ? 'unavailable' : 'none')
      : ['planned', 'unavailable', 'none'].indexOf(p.dayAheadState) >= 0 ? p.dayAheadState
      : Object.keys(p.values || {}).some(function (k) { return k.indexOf('day_ahead_') === 0; }) ? 'unavailable' : 'none';
    var dirs = disc[p.start] || p.directions, vlag = !!p.afrr || events.has(p.start);
    var ai = Array.isArray(dirs) ? dirs.indexOf('import') >= 0 : vlag, ae = Array.isArray(dirs) ? dirs.indexOf('export') >= 0 : vlag;
    var mi = state === 'unavailable' ? null : state === 'planned' ? (ai ? 'day_ahead_afrr_import' : 'day_ahead_import') : (ai ? 'afrr_import' : 'onbalans_import');
    var me = ae ? 'afrr_export' : 'onbalans_export', v = p.values || {};
    return { mi: mi, me: me, ki: mi && getal(v[mi]) ? v[mi] : null, ke: getal(v[me]) ? v[me] : null };
  }
  function planning(j) {
    var plans = j && j.dayAheadPlans, windows = j && j.dayAheadUnavailableWindows, planned = new Set();
    (Array.isArray(plans) ? plans : []).forEach(function (p) { if (p && getal(p.start) && p.direction === 'import') planned.add(p.start); });
    var w = (Array.isArray(windows) ? windows : []).filter(function (x) { return Array.isArray(x) && getal(x[0]) && getal(x[1]) && x[1] > x[0]; });
    return { authoritative: Array.isArray(plans) || Array.isArray(windows), planned: planned,
      onbekend: function (s) { return w.some(function (x) { return s >= x[0] && s < x[1]; }); } };
  }

  if (window.__BEMB_TEST__) { window.__BEMB_TEST__ = { verdeel: verdeel, naarKwartieren: naarKwartieren, waardeer: waardeer, verdeelDefinitief: verdeelDefinitief, allin: allin, grenzen: grenzen, isoAms: isoAms, prijsKeuze: prijsKeuze, planning: planning, parseDefinitief: parseDefinitief, kiesPrijzen: kiesPrijzen, allinMetingen: allinMetingen, kalibreer: kalibreer, naarAC: naarAC, ETA_STANDAARD: ETA_STANDAARD, dagPayload: dagPayload, maakLabel: maakLabel, leesLabel: leesLabel, magOverschrijven: magOverschrijven, kwaliteit: kwaliteit, nauwkeuriger: nauwkeuriger, vulOnbalans: vulOnbalans, tenergyAanvullen: tenergyAanvullen, prijsKeuze: prijsKeuze, zetActief: zetActief, tenergyVast: tenergyVast, zetPrijzen: zetPrijzen, parseDefinitief: parseDefinitief, marktPrijs: marktPrijs, extraVelden: extraVelden, dagPayload: dagPayload, metTennet: metTennet }; return; }
  // Diagnose (alleen als localStorage 'bemb_debug' = '1'): rekenfuncties en gegevens bereikbaar via window.__bemb.
  try {
    if (localStorage.getItem('bemb_debug') === '1') window.__bemb = Object.assign({ verdeel: verdeel, naarKwartieren: naarKwartieren, waardeer: waardeer, verdeelDefinitief: verdeelDefinitief, allin: allin, grenzen: grenzen, isoAms: isoAms, prijsKeuze: prijsKeuze, planning: planning, parseDefinitief: parseDefinitief, kiesPrijzen: kiesPrijzen, allinMetingen: allinMetingen, kalibreer: kalibreer, naarAC: naarAC, ETA_STANDAARD: ETA_STANDAARD, dagPayload: dagPayload, maakLabel: maakLabel, leesLabel: leesLabel, magOverschrijven: magOverschrijven, kwaliteit: kwaliteit, nauwkeuriger: nauwkeuriger, vulOnbalans: vulOnbalans, tenergyAanvullen: tenergyAanvullen, prijsKeuze: prijsKeuze, zetActief: zetActief, tenergyVast: tenergyVast, zetPrijzen: zetPrijzen, parseDefinitief: parseDefinitief, marktPrijs: marktPrijs, extraVelden: extraVelden, dagPayload: dagPayload }, {
      cacheKwartieren: cacheKwartieren, haalDefinitief: haalDefinitief, prijsIndex: prijsIndex, tennetPrijzen: tennetPrijzen, prijzenDag: prijzenDag, brug: brug, klant: klant, dagDefinitief: dagDefinitief,
      maandBron: maandBron, rijVan: rijVan, cfg: cfg, lees: lees, k: k, versie: VERSIE });
  } catch (e) {}

  /* ── Installatie en instellingen ── */
  function geldig(id) { return !!id && /^[A-Za-z0-9_-]+$/.test(id) && !/^(?:account|results|current-history|current-prices|undefined|null)$/i.test(id); }
  function klant() {
    var m = location.pathname.match(/^\/customer\/([^/]+)(?:\/|$)/);
    if (m && geldig(m[1])) return m[1];
    try { var id = localStorage.getItem('be_laatste_installatie'); if (geldig(id)) return id; } catch (e) {}
    var links = document.querySelectorAll('a[href*="/customer/"]');
    for (var i = 0; i < links.length; i++) { var mm = links[i].pathname.match(/^\/customer\/([^/]+)\/(?:results|account)(?:\/|$)/); if (mm && geldig(mm[1])) return mm[1]; }
    return '';
  }
  var CFG = 'bemb_cfg_v1';
  function cfg() {
    var d = { auto: false, proef: true, saldeert: true, afrr: true, historieVan: '', afstemming: 'auto' };
    try { Object.assign(d, JSON.parse(localStorage.getItem(CFG) || '{}')); } catch (e) {}
    return d;
  }
  function zetCfg(k, v) { var c = cfg(); c[k] = v; try { localStorage.setItem(CFG, JSON.stringify(c)); } catch (e) {} }

  /* ── Opslag (eigen IndexedDB, los van All-in) ── */
  var dbP = null;
  function db() {
    if (!dbP) dbP = new Promise(function (res, rej) {
      var r = indexedDB.open('be-mijnbatterij', 1);
      r.onupgradeneeded = function () { r.result.createObjectStore('kv'); };
      r.onsuccess = function () { res(r.result); }; r.onerror = function () { rej(r.error); };
    });
    return dbP;
  }
  async function lees(k) { var d = await db(); return new Promise(function (res, rej) { var q = d.transaction('kv', 'readonly').objectStore('kv').get(k); q.onsuccess = function () { res(q.result); }; q.onerror = function () { rej(q.error); }; }); }
  async function schrijf(k, v) { var d = await db(); return new Promise(function (res, rej) { var tx = d.transaction('kv', 'readwrite'); if (v === undefined) tx.objectStore('kv').delete(k); else tx.objectStore('kv').put(v, k); tx.oncomplete = function () { res(); }; tx.onerror = tx.onabort = function () { rej(tx.error || Error('Opslaan mislukt')); }; }); }
  function k(id, s) { return id + ':' + s; }

  /* ── Logboek ── */
  var log = [];
  function noteer(s) { log.unshift(klok(Date.now()) + '  ' + s); log = log.slice(0, 60); var n = shadow && shadow.getElementById('log'); if (n) n.textContent = log.join('\n'); }

  /* ── BE-verzoeken (eigen XHR; deelt de 429-pauze met All-in) ── */
  function beGet(path, params) {
    return new Promise(function (resolve, reject) {
      var until = Number(localStorage.getItem('be_api_retry_until') || 0);
      if (Date.now() < until) { var p = Error('BE-verzoeklimiet actief tot ' + klok(until)); p.pauze = true; reject(p); return; }
      var u = new URL(path, location.origin);
      Object.keys(params || {}).forEach(function (x) { u.searchParams.set(x, params[x]); });
      var x = new XMLHttpRequest();
      x.open('GET', u.href); x.withCredentials = true; x.timeout = 30000; x.setRequestHeader('Accept', 'application/json');
      x.onload = function () {
        if (x.status === 429) {
          var ra = x.getResponseHeader('Retry-After'), s = Number(ra), d = ra ? (isFinite(s) ? s * 1000 : Date.parse(ra) - Date.now()) : 60000;
          if (!isFinite(d) || d < 1000) d = 60000;
          try { localStorage.setItem('be_api_retry_until', String(Date.now() + d)); } catch (e) {}
          var e = Error('BE beperkt verzoeken (HTTP 429)'); e.pauze = true; reject(e); return;
        }
        if (x.status < 200 || x.status >= 300) { reject(Error('BE HTTP ' + x.status)); return; }
        try { resolve(JSON.parse(x.responseText)); } catch (e) { reject(Error('BE gaf geen JSON')); }
      };
      x.onerror = function () { reject(Error('Netwerkfout naar BE')); };
      x.ontimeout = function () { reject(Error('Timeout naar BE')); };
      x.send();
    });
  }

  /* ── Brug-aanroepen ── */
  var brugRij = Promise.resolve();
  function brug(msg) {
    function run() {
      return new Promise(function (resolve, reject) {
        var id = 'b' + Date.now() + Math.random().toString(36).slice(2);
        var out = document.getElementById('bemb-out');
        if (!out) { out = document.createElement('script'); out.type = 'application/json'; out.id = 'bemb-out'; document.documentElement.appendChild(out); }
        var timer = setTimeout(function () { klaar(); reject(Error('Geen antwoord van de userscriptlaag')); }, 35000);
        function klaar() { clearTimeout(timer); document.removeEventListener('bemb-answer', ant); }
        function ant() { var a; try { a = JSON.parse(document.getElementById('bemb-in').textContent); } catch (e) { return; } if (a.id !== id) return; klaar(); resolve(a); }
        document.addEventListener('bemb-answer', ant);
        out.textContent = JSON.stringify(Object.assign({ id: id }, msg));
        document.dispatchEvent(new Event('bemb-send'));
      });
    }
    var p = brugRij.then(run, run); brugRij = p.catch(function () {}); return p;
  }
  async function mb(method, path, body) { var a = await brug({ op: 'api', method: method, path: path, body: body || null }); if (!a.ok) throw Error(a.error || 'HTTP ' + a.status); return a; }
  var mbOpgeslagen = {}, mbMaanden = {}, mbOpgehaald = 0;
  async function vergelijkMijnbatterij() {
    var tot = plusDagen(vandaag(), -1), van = plusDagen(vandaag(), -DAG_VENSTER);
    var a = await mb('GET', '/api/results/daily?from=' + van + '&to=' + tot), j;
    try { j = JSON.parse(a.text); } catch (e) { throw Error('Mijnbatterij gaf geen JSON'); }
    var lijst = Array.isArray(j) ? j : (j && (j.results || j.data || j.days)) || [];
    if (!Array.isArray(lijst)) throw Error('Onbekend antwoord van Mijnbatterij');
    mbOpgeslagen = {};
    lijst.forEach(function (r) { var d = r && String(r.date || r.day || '').slice(0, 10); if (/^\d{4}-\d{2}-\d{2}$/.test(d)) mbOpgeslagen[d] = r; });
    mbMaanden = {};
    for (var ym of Array.from(new Set([van.slice(0, 7), tot.slice(0, 7)]))) {
      var b = await mb('GET', '/api/results/monthly?from=' + ym + '&to=' + ym), mj;
      try { mj = JSON.parse(b.text); } catch (e) { continue; }
      ((mj && mj.results) || []).forEach(function (r) { var key = String(r.yearMonth || r.date || '').slice(0, 7); if (key === ym) mbMaanden[ym] = r; });
    }
    mbOpgehaald = Date.now();
    return Object.keys(mbOpgeslagen).length;
  }
  function num(v) { var n = typeof v === 'string' ? Number(v) : v; return getal(n) ? n : null; }
  var heeftSleutel = false;
  async function sleutelStatus() { try { heeftSleutel = !!(await brug({ op: 'key-status' })).hasKey; } catch (e) { heeftSleutel = false; } return heeftSleutel; }

  /* ── Verzamelen: ruwe metingen (26 uur) en prijzen ── */
  async function verzamelRauw(id) {
    var nu = Date.now(), lk = 'bemb_rauw_tot:' + id, laatst = Number(localStorage.getItem(lk) || 0);
    var van = Math.max(nu - 26 * 3600000, laatst ? laatst - 20 * 60000 : 0);
    var j = await beGet('/customer/current-history/api/', { installation: id, start: String(van), end: String(nu) });
    var res = j && getal(j.resolutionMs) ? j.resolutionMs : 0, perDag = {}, max = laatst;
    ((j && j.rows) || []).forEach(function (r) {
      if (!r || !getal(r.t) || !getal(r.po) || r.t < van - 60000 || r.t > nu + 60000) return;
      var n = { t: r.t, po: r.po, r: res };
      if (getal(r.bo)) n.bo = r.bo;
      if (getal(r.s)) n.s = r.s;
      ['l', 'p', 'g'].forEach(function (f) { if (getal(r[f])) n[f] = r[f]; });
      (perDag[dagVan(r.t)] = perDag[dagVan(r.t)] || []).push(n);
      if (r.t > max) max = r.t;
    });
    var index = (await lees(k(id, 'rauw-index'))) || { dagen: [] };
    for (var dk of Object.keys(perDag)) {
      var oud = ((await lees(k(id, 'rauw:' + dk))) || {}).rows || [], map = new Map(oud.map(function (r) { return [r.t, r]; }));
      // Fijnere resolutie wint; een grove meting overschrijft nooit een fijne.
      perDag[dk].forEach(function (r) {
        var o = map.get(r.t);
        if (!o) { map.set(r.t, r); return; }
        if (getal(o.bo) && !getal(r.bo)) return;
        if (getal(o.bo) && getal(o.r) && o.r < r.r) return;
        map.set(r.t, r);
      });
      await schrijf(k(id, 'rauw:' + dk), { rows: Array.from(map.values()).sort(function (a, b) { return a.t - b.t; }) });
      if (index.dagen.indexOf(dk) < 0) index.dagen.push(dk);
    }
    var grens = plusDagen(vandaag(), -RAUW_DAGEN);
    for (var d of index.dagen.filter(function (x) { return x < grens; })) await schrijf(k(id, 'rauw:' + d), undefined);
    index.dagen = index.dagen.filter(function (x) { return x >= grens; }).sort();
    await schrijf(k(id, 'rauw-index'), index);
    if (max > laatst) try { localStorage.setItem(lk, String(max)); } catch (e) {}
    await werkRendementBij(id, index.dagen);
  }
  async function werkRendementBij(id, dagen) {
    var alle = [];
    for (var d of dagen) alle = alle.concat(((await lees(k(id, 'rauw:' + d))) || {}).rows || []);
    ETA = kalibreer(alle);
    try { localStorage.setItem('bemb_eta:' + id, JSON.stringify(ETA)); } catch (e) {}
  }
  function laadRendement(id) { try { var e = JSON.parse(localStorage.getItem('bemb_eta:' + id) || 'null'); if (e && e.uit && e.in) ETA = e; } catch (x) {} }
  async function verzamelPrijzen(id) {
    await verwerkPrijzen(id, await beGet('/customer/current-prices/api/'));
    // Herstel: gaten van gisteren en vandaag gericht ophalen, hooguit eens per uur per dag.
    for (var dk of [plusDagen(vandaag(), -1), vandaag()]) { try { await herstelPrijzenDag(id, dk, 3600000); } catch (e) { if (e.pauze) throw e; noteer('Prijzen ' + dk + ': ' + e.message); } }
  }
  function prijsGaten(arch, g, tot) {
    var mist = 0;
    for (var q = g.start; q < tot; q += Q) if (!arch[q] || (!getal(arch[q].ki) && arch[q].mi) || !getal(arch[q].ke)) mist++;
    return mist;
  }
  // Kwartierprijzen van een dag gericht bij BE ophalen (start/end). De pauze
  // geldt pas na een geslaagde aanvraag, zodat een mislukte poging terugkomt.
  async function herstelPrijzenDag(id, dk, pauze) {
    var g = grenzen(dk), nu = Date.now(), tot = Math.min(g.eind, Math.floor(nu / Q) * Q), hk = 'bemb_prijsherstel:' + id + ':' + dk;
    if (tot <= g.start) return 0;
    var arch = ((await lees(k(id, 'prijzen'))) || {}).rows || {}, voor = prijsGaten(arch, g, tot);
    if (!voor || nu - Number(localStorage.getItem(hk) || 0) < pauze) return voor;
    await wacht(800);
    var j;
    try { j = await beGet('/customer/current-prices/api/', { start: String(g.start), end: String(tot) }); }
    catch (e) {
      // BE weigert sommige periodes (HTTP 400): dan vandaag niet steeds opnieuw.
      if (/HTTP 400/.test(e.message)) try { localStorage.setItem(hk, String(Date.now() + 20 * 3600000)); } catch (x) {}
      throw e;
    }
    await verwerkPrijzen(id, j);
    try { localStorage.setItem(hk, String(Date.now())); } catch (e) {}
    var na = prijsGaten(((await lees(k(id, 'prijzen'))) || {}).rows || {}, g, tot);
    noteer('Prijzen ' + dk + ': ' + (voor - na) + ' kwartieren hersteld uit BE' + (na ? ', ' + na + ' nog zonder prijs (BE leverde ' + ((j && j.prices) || []).length + ' rijen)' : '') + '.');
    return na;
  }
  // Sinds 17-11-2026 geeft BE alleen nog prijzen van kwartieren waarin de accu iets deed.
  // Balansenergie All-in vult onbalansprijzen aan via Tenergy (localStorage, zelfde site):
  // die lezen we alleen. Rangorde: BE > Tenergy.
  function tenergyCache() {
    try { var o = JSON.parse(localStorage.getItem('be_onbalans_tenergy_v1') || '{}'); return o && typeof o === 'object' && !Array.isArray(o) ? o : {}; }
    catch (e) { return {}; }
  }
  function vulOnbalans(values, t) {
    var v = Object.assign({}, values || {});
    if (t) {
      if (!getal(v.onbalans_import) && getal(t.ki)) v.onbalans_import = t.ki;
      if (!getal(v.onbalans_export) && getal(t.ke)) v.onbalans_export = t.ke;
    }
    return v;
  }
  // Vastgesteld = na afloop van het kwartier bepaald: door BE zelf (activiteit/aFRR), of door
  // Tenergy met de regeltoestand uit de activatiedata (zoals het BE-dashboard). Zonder
  // activatiedata is het alleen de prijs van de laatste minuut: bruikbaar, niet vastgesteld.
  function tenergyVast(t, q, nu) { return !!(t && t.klaar && t.rs !== null && t.rs !== undefined && q + Q <= nu); }
  function tenergyAanvullen(arch, ten, nu, van) {
    var n = 0;
    Object.keys(ten || {}).forEach(function (k) {
      var q = +k, t = ten[k];
      if (!(q >= van) || q > nu || q % Q || !t) return;
      var a = arch[q], tv = tenergyVast(t, q, nu);
      if (!a) { arch[q] = { mi: 'onbalans_import', me: 'onbalans_export', ki: getal(t.ki) ? t.ki : null, ke: getal(t.ke) ? t.ke : null, v: !t.klaar || q + Q > nu, t: nu, ti: true, te: true, vi: tv && getal(t.ki), ve: tv && getal(t.ke) }; n++; return; }
      var g = false;
      // Ontbreekt de prijs, of kwam hij eerder (nog niet vastgesteld) van Tenergy: bijwerken.
      if (a.mi === 'onbalans_import' && getal(t.ki) && (!getal(a.ki) || (a.ti && !a.vi))) { a.ki = t.ki; a.ti = true; a.vi = tv; g = true; }
      if (a.me === 'onbalans_export' && getal(t.ke) && (!getal(a.ke) || (a.te && !a.ve))) { a.ke = t.ke; a.te = true; a.ve = tv; g = true; }
      if (g) n++;
    });
    return n;
  }
  async function verwerkPrijzen(id, j) {
    if (!j || !Array.isArray(j.prices)) return;
    var ten = tenergyCache();
    var ctx = planning(j), events = new Set(j.afrrEventIsps || []), disc = {}, nu = Date.now();
    (j.afrrDiscovery || []).forEach(function (d) { if (d && getal(d.start) && Array.isArray(d.directions)) disc[d.start] = d.directions; });
    var arch = ((await lees(k(id, 'prijzen'))) || {}).rows || {};
    j.prices.forEach(function (p) {
      if (!p || !getal(p.start) || p.start % Q || p.carried) return;
      var orig = p.values || {}, t = ten[p.start], tv = tenergyVast(t, p.start, nu), af = p.start + Q <= nu;
      p = Object.assign({}, p, { values: vulOnbalans(orig, t) });
      var keus = prijsKeuze(p, ctx, events, disc), oud = arch[p.start] || {};
      if (keus.ki === null && keus.ke === null && arch[p.start]) return; // BE gaf niets meer: vastgelegde prijs houden
      var tijdelijk = !!p.temporaryTariffs || (p.start <= nu && nu < p.start + Q);
      var vanBEi = keus.ki !== null && getal(orig[keus.mi]), vanBEe = keus.ke !== null && getal(orig[keus.me]);
      var houdI = keus.ki === null && oud.mi === keus.mi, houdE = keus.ke === null && oud.me === keus.me;
      arch[p.start] = {
        mi: keus.mi, me: keus.me,
        ki: keus.ki !== null ? keus.ki : (houdI ? oud.ki : null),
        ke: keus.ke !== null ? keus.ke : (houdE ? oud.ke : null),
        v: !!p.provisional || tijdelijk, t: nu,
        ti: keus.ki !== null ? !vanBEi : (houdI ? !!oud.ti : false), te: keus.ke !== null ? !vanBEe : (houdE ? !!oud.te : false),
        vi: keus.ki !== null ? (vanBEi ? af : tv) : (houdI ? !!oud.vi : false), ve: keus.ke !== null ? (vanBEe ? af : tv) : (houdE ? !!oud.ve : false)
      };
    });
    tenergyAanvullen(arch, ten, nu, nu - 2 * 86400000);
    var grens = nu - PRIJS_DAGEN * 86400000;
    Object.keys(arch).forEach(function (s) { if (+s < grens) delete arch[s]; });
    await schrijf(k(id, 'prijzen'), { rows: arch });
  }

  /* ── Dagberekening ── */
  async function cacheKwartieren(id, dk) {
    var g = grenzen(dk); if (!g) return {};
    var rows = ((await lees(k(id, 'rauw:' + dk))) || {}).rows || [];
    var vorige = ((await lees(k(id, 'rauw:' + plusDagen(dk, -1)))) || {}).rows || [];
    if (vorige.length) rows = [vorige[vorige.length - 1]].concat(rows);
    var kw = naarKwartieren(rows), arch = naarKwartieren(await allinMetingen(id, dk)), uit = {};
    // Per kwartier de bron met de meeste accudekking; bij gelijke dekking de eigen meting.
    Object.keys(kw).concat(Object.keys(arch)).forEach(function (q) {
      if (+q < g.start || +q >= g.eind || uit[q]) return;
      var a = kw[q], b = arch[q];
      uit[q] = !a ? b : !b ? a : ((b.accuSec || 0) > (a.accuSec || 0) + 1 ? b : a);
    });
    return uit;
  }
  /* Metingen uit Balansenergie All-in (ALLEEN LEZEN): accuvermogen + SOC uit de
   * coulombtelling (be-own-energy-soc-v1, 30 dagen) en netvermogen uit het
   * bronarchief (balansenergie-bronnen-v1, per dag). Zelfde tijdstempels uit
   * dezelfde BE-historie; samengevoegd tot één tijdlijn. Gaten tot 5 min (de
   * pollinterval van All-in) worden overbrugd, langere niet. */
  async function allinMetingen(id, dk) {
    var g = grenzen(dk); if (!g) return [];
    if (coulombCache.id !== id || Date.now() - coulombCache.at > 60000) {
      var c = await leesAllinDb('be-own-energy-soc-v1', 'installations', id);
      coulombCache = { at: Date.now(), id: id, rows: ((c && c.rows) || []).filter(function (r) { return Array.isArray(r) && getal(r[0]) && getal(r[2]); }) };
    }
    var van = g.start - 600000, tot = g.eind + 600000;
    var C = coulombCache.rows.filter(function (r) { return r[0] >= van && r[0] < tot; });
    if (!C.length) return [];
    var N = [];
    for (var d of [plusDagen(dk, -1), dk, plusDagen(dk, 1)]) {
      var n = await leesAllinDb('balansenergie-bronnen-v1', 'dagen', id + ':' + d);
      ((n && n.rows) || []).forEach(function (r) { if (r && getal(r.t) && getal(r.po) && r.t >= van && r.t < tot) N.push(r); });
    }
    if (!N.length) return [];
    N.sort(function (a, b) { return a.t - b.t; });
    var T = Array.from(new Set(C.map(function (r) { return r[0]; }).concat(N.map(function (r) { return r.t; })))).sort(function (a, b) { return a - b; });
    var ci = 0, ni = 0, uit = [];
    T.forEach(function (t) {
      while (ci + 1 < C.length && C[ci + 1][0] <= t) ci++;
      while (ni + 1 < N.length && N[ni + 1].t <= t) ni++;
      var c = C[ci], n = N[ni];
      if (!c || c[0] > t || t - c[0] > ARCHIEF_GAT || !n || n.t > t || t - n.t > ARCHIEF_GAT) return;
      var o = { t: t, po: n.po, bo: c[2], r: ARCHIEF_GAT / 1.5 };
      if (getal(c[1])) o.s = c[1];
      uit.push(o);
    });
    return uit;
  }
  // Prijsarchief van Balansenergie All-in (6.8.x), ALLEEN LEZEN. Bestaat de
  // database niet, dan wordt het aanmaken direct afgebroken: All-in moet zijn
  // eigen database met eigen store kunnen aanmaken.
  var allinCache = { at: 0, id: '', rows: {} };
  function leesAllinArchief(id) { return leesAllinDb('balansenergie-bronnen-v1', 'dagen', id + ':prijzen'); }
  function leesAllinDb(naam, store, sleutelWaarde) {
    return new Promise(function (resolve) {
      var klaar = false, r;
      function uit(v) { if (!klaar) { klaar = true; resolve(v); } }
      try { r = indexedDB.open(naam); } catch (e) { uit(null); return; }
      r.onupgradeneeded = function () { try { r.transaction.abort(); } catch (e) {} };
      r.onerror = function () { uit(null); }; r.onblocked = function () { uit(null); };
      r.onsuccess = function () {
        var d = r.result;
        try {
          if (!d.objectStoreNames.contains(store)) { d.close(); uit(null); return; }
          var q = d.transaction(store, 'readonly').objectStore(store).get(sleutelWaarde);
          q.onsuccess = function () { d.close(); uit(q.result || null); };
          q.onerror = function () { d.close(); uit(null); };
        } catch (e) { try { d.close(); } catch (x) {} uit(null); }
      };
      setTimeout(function () { uit(null); }, 5000);
    });
  }
  var LABEL = { onbalans_import: 'onbalans', afrr_import: 'aFRR', day_ahead_import: 'Dynamisch', day_ahead_afrr_import: 'Dynamisch + aFRR', onbalans_export: 'onbalans', afrr_export: 'aFRR' };
  async function allinPrijzen(id) {
    if (allinCache.id === id && Date.now() - allinCache.at < 120000) return allinCache.rows;
    var bron = await leesAllinArchief(id), uit = {}, leeg = { authoritative: false, planned: new Set(), onbekend: function () { return false; } }, ten = tenergyCache(), nu = Date.now();
    ((bron && bron.rows) || []).forEach(function (p) {
      if (!p || !getal(p.start) || p.start % Q || p.carried) return;
      var keus = prijsKeuze(p, leeg, new Set(), {}), ki = keus.ki, ke = keus.ke, t = ten[p.start], af = p.start + Q <= nu;
      // All-in kan aangevulde Tenergy-waarden in het archief bewaren: herken ze aan de cache.
      var vi = getal(ki) && af && (t && keus.mi === 'onbalans_import' && ki === t.ki ? tenergyVast(t, p.start, nu) : true);
      var ve = getal(ke) && af && (t && keus.me === 'onbalans_export' && ke === t.ke ? tenergyVast(t, p.start, nu) : true);
      // Ontbrekende quote: laatste live-waarneming van dezelfde markt (niet vastgesteld).
      (Array.isArray(p.verloop) ? p.verloop : []).slice().reverse().forEach(function (o) {
        if (!getal(ki) && keus.mi && o && o.marktImport === LABEL[keus.mi] && getal(o.ki)) { ki = o.ki; vi = false; }
        if (!getal(ke) && o && o.marktExport === LABEL[keus.me] && getal(o.ke)) { ke = o.ke; ve = false; }
      });
      if (getal(ki) || getal(ke)) uit[p.start] = { ki: getal(ki) ? ki : null, ke: getal(ke) ? ke : null, mi: keus.mi, me: keus.me, v: p.provisional !== false, allin: true, vi: !!vi, ve: !!ve };
    });
    allinCache = { at: Date.now(), id: id, rows: uit };
    return uit;
  }
  // Eigen archief eerst; ontbrekende kwartieren aangevuld uit het All-in-archief.
  async function prijsIndex(id) {
    var eigen = ((await lees(k(id, 'prijzen'))) || {}).rows || {}, extra = await allinPrijzen(id), uit = {};
    Object.keys(extra).forEach(function (q) { uit[q] = extra[q]; });
    Object.keys(eigen).forEach(function (q) {
      var a = eigen[q], b = uit[q];
      // Per veld: vastgestelde prijs gaat voor; anders de eigen waarneming, dan All-in.
      var kiesI = !b || (getal(a.ki) && (a.vi || !b.vi || !getal(b.ki))), kiesE = !b || (getal(a.ke) && (a.ve || !b.ve || !getal(b.ke)));
      uit[q] = !b ? a : { ki: kiesI ? a.ki : b.ki, vi: kiesI ? !!a.vi : !!b.vi, ke: kiesE ? a.ke : b.ke, ve: kiesE ? !!a.ve : !!b.ve, mi: a.mi, me: a.me, v: a.v && b.v };
    });
    return uit;
  }
  // TenneT-settlementprijzen (tekort/overschot per kwartier, gepubliceerd na afloop van de dag).
  // Getoetst tegen BE's market_breakdown: tot op de cent gelijk aan wat BE als onbalans afrekent.
  // Wereldwijd per dag bewaard (niet per klant). Alleen afgelopen dagen.
  async function tennetPrijzen(dk, alleenCache) {
    var g = grenzen(dk); if (!g || dk >= vandaag()) return {};
    var sk = 'nl:tennet:' + dk, st = await lees(sk), nu = Date.now(), n = (g.eind - g.start) / Q;
    if (st && st.v === 2 && (st.compleet || (alleenCache && st.rows) || nu - st.at < (st.rows ? 6 : 1) * 3600000)) return st.rows || {};
    if (alleenCache) return (st && st.rows) || {};
    var a; try { a = await brug({ op: 'tennet', datum: dk.slice(8, 10) + '-' + dk.slice(5, 7) + '-' + dk.slice(0, 4) }); } catch (e) { a = { ok: false, error: e.message }; }
    if (!a || !a.ok) { noteer('TenneT-prijzen ' + dk + ': ' + ((a && a.error) || 'geen antwoord')); await schrijf(sk, { v: 2, at: nu, fout: (a && a.error) || 'geen antwoord' }); return {}; }
    var rows = {}, c = 0;
    Object.keys(a.rijen || {}).forEach(function (i) {
      var isp = +i, r = a.rijen[i];
      if (!(isp >= 1 && isp <= n) || !Array.isArray(r)) return;
      var ki = getal(r[0]) && Math.abs(r[0] / 1000) <= MAX_SETTLE ? r[0] / 1000 : null, ke = getal(r[1]) && Math.abs(r[1] / 1000) <= MAX_SETTLE ? r[1] / 1000 : null;
      if (ki === null && ke === null) return;
      rows[g.start + (isp - 1) * Q] = { ki: ki, ke: ke }; if (ki !== null && ke !== null) c++;
    });
    // Een dag zonder (volledige) publicatie later opnieuw proberen; een complete dag verandert niet meer.
    if (!c) { await schrijf(sk, { v: 2, at: nu, fout: 'nog niet gepubliceerd' }); return {}; }
    await schrijf(sk, { v: 2, at: nu, rows: rows, compleet: c === n });
    if (c < n) noteer('TenneT-prijzen ' + dk + ': ' + c + ' van ' + n + ' kwartieren gepubliceerd.');
    return rows;
  }
  // Onbalansprijs uit het archief (of ontbrekend) vervangen door TenneT's vastgestelde prijs.
  // Prijzen van een andere markt (day-ahead, aFRR) blijven staan: die zijn afgerekend zoals BE ze gaf.
  function metTennet(prijzen, tn) {
    var keys = Object.keys(tn || {}); if (!keys.length) return prijzen;
    var uit = Object.assign({}, prijzen);
    keys.forEach(function (q) {
      var t = tn[q], a = uit[q];
      var neemI = getal(t.ki) && (!a || !getal(a.ki) || ((a.mi === 'onbalans_import' || !a.mi) && (!a.vi || a.ti)));
      var neemE = getal(t.ke) && (!a || !getal(a.ke) || ((a.me === 'onbalans_export' || !a.me) && (!a.ve || a.te)));
      if (!neemI && !neemE) return;
      var b = Object.assign({}, a || {});
      if (neemI) { b.ki = t.ki; b.vi = true; b.ti = false; b.tnI = true; b.mi = 'onbalans_import'; }
      if (neemE) { b.ke = t.ke; b.ve = true; b.te = false; b.tnE = true; b.me = 'onbalans_export'; }
      b.v = !!(a && a.v) && !(neemI && neemE);
      uit[q] = b;
    });
    return uit;
  }
  // Status van de TenneT-prijzen van één afgelopen dag: compleet, of waarom niet.
  async function tennetStatus(dk) {
    await tennetPrijzen(dk);
    var st = await lees('nl:tennet:' + dk);
    if (st && st.compleet) return { ok: true };
    return { ok: false, fout: (st && (st.fout || (st.rows ? 'nog niet alle kwartieren gepubliceerd' : ''))) || 'geen antwoord' };
  }
  async function prijzenDag(id, dk, alleenCache) { return metTennet(await prijsIndex(id), await tennetPrijzen(dk, alleenCache)); }
  function zetPrijzen(kw, prijzen) {
    Object.keys(kw).forEach(function (q) {
      var p = prijzen[q], x = kw[q];
      if (!getal(x.pIn) && p) { x.pIn = p.ki; x.pInVast = !!p.vi; }
      if (!getal(x.pUit) && p) { x.pUit = p.ke; x.pUitVast = !!p.ve; }
      if (p && p.v) x.prijsVoorlopig = true;
    });
    return kw;
  }
  async function dagUitCache(id, dk, tot, c, alleenCache) {
    var g = grenzen(dk), eind = Math.min(g.eind, tot || g.eind), duur = Math.max(1, (eind - g.start) / 1000);
    var kw = zetPrijzen(await cacheKwartieren(id, dk), await prijzenDag(id, dk, alleenCache));
    Object.keys(kw).forEach(function (q) { if (+q >= eind) delete kw[q]; });
    var w = waardeer(kw, c, null);
    return { dk: dk, bron: 'cache', kw: kw, w: w, eta: ETA, dekking: Math.min(1, w.accuSec / duur), prijsDekking: Math.min(1, w.prijsSec / duur), kwal: Math.min(1, w.vastSec / duur) };
  }
  function parseDefinitief(j, rij, dk) {
    var lijst = j && j.day_detail && j.day_detail.intervals, g = grenzen(dk), out = {}, laden = 0, ontladen = 0, leegAccu = 0;
    if (!j || j.interval !== 'day' || j.period !== dk || !j.pricing || j.pricing.include_vat !== false || j.pricing.include_delivery_cost !== false) throw Error('BE bevestigt dag of kale prijzen niet');
    if (!Array.isArray(lijst) || !g) throw Error('Geen kwartierdata');
    lijst.forEach(function (v) {
      var q = typeof v.timestamp === 'number' ? v.timestamp : typeof v.timestamp === 'string' && /(Z|[+-]\d{2}:\d{2})$/.test(v.timestamp) ? Date.parse(v.timestamp) : NaN;
      if (!getal(q) || q % Q || q < g.start || q >= g.eind || out[q]) throw Error('Onbekende of dubbele kwartiertijd');
      var f = ['grid_import_kwh', 'grid_export_kwh'].filter(function (x) { return !getal(v[x]) || v[x] < 0; });
      if (f.length) throw Error('Ontbrekend: ' + f.join(', ') + ' om ' + klok(q));
      // Een los kwartier zonder accuwaarden (gezien om 00:00) telt als 0; het
      // dagtotaal hieronder bewaakt dat er niets wezenlijks verdwijnt.
      ['battery_charged_kwh', 'battery_discharged_kwh'].forEach(function (x) { if (v[x] != null && (!getal(v[x]) || v[x] < 0)) throw Error('Ongeldig: ' + x + ' om ' + klok(q)); });
      var bl = getal(v.battery_charged_kwh) ? v.battery_charged_kwh : 0, bo = getal(v.battery_discharged_kwh) ? v.battery_discharged_kwh : 0;
      var leeg = !getal(v.battery_charged_kwh) || !getal(v.battery_discharged_kwh);
      if (leeg) leegAccu++;
      laden += bl; ontladen += bo;
      // Settlementprijs per kwartier uit market_breakdown (per markt, onafhankelijk van het volume);
      // de effectieve prijs (kosten/kWh) blijft de reserve.
      var mb = v.market_breakdown && v.market_breakdown.available ? v.market_breakdown : null;
      out[q] = { imp: v.grid_import_kwh, exp: v.grid_export_kwh, laden: bl, ontladen: bo, leeg: leeg,
        pIn: getal(v.effective_import_price_eur_per_kwh) ? v.effective_import_price_eur_per_kwh : null,
        pUit: getal(v.effective_export_price_eur_per_kwh) ? v.effective_export_price_eur_per_kwh : null,
        mIn: mb ? marktPrijs(mb.purchases) : null, mUit: mb ? marktPrijs(mb.sales) : null,
        imbIn: mb ? marktPrijs(mb.purchases, 'imbalance') : null, imbUit: mb ? marktPrijs(mb.sales, 'imbalance') : null,
        pv: getal(v.solar_direct_kwh) && v.solar_direct_kwh >= 0 ? v.solar_direct_kwh : null,
        load: getal(v.load_kwh) && v.load_kwh >= 0 ? v.load_kwh : null };
    });
    if (Object.keys(out).length !== (g.eind - g.start) / Q) throw Error('Kwartieren dekken niet de hele dag');
    if (rij && getal(rij.battery_charged_kwh) && getal(rij.battery_discharged_kwh) &&
      (Math.abs(laden - rij.battery_charged_kwh) > 0.15 || Math.abs(ontladen - rij.battery_discharged_kwh) > 0.15)) throw Error('Kwartieren sluiten niet aan op dagtotaal');
    return out;
  }
  // Volumegewogen prijs van de markttransacties in één richting (kaal, €/kWh); null als er geen zijn.
  function marktPrijs(lijst, markt) {
    var v = 0, e = 0;
    (Array.isArray(lijst) ? lijst : []).forEach(function (x) { if (x && (!markt || x.market === markt) && getal(x.volume_kwh) && x.volume_kwh > 0 && getal(x.price_eur_per_kwh)) { v += x.volume_kwh; e += x.volume_kwh * x.price_eur_per_kwh; } });
    return v > 0 ? e / v : null;
  }
  function afrrVan(rij) { if (!rij) return null; var a = rij.balance_energie_eur, b = rij.deal_customer_share_eur; return getal(a) || getal(b) ? (getal(a) ? a : 0) + (getal(b) ? b : 0) : null; }
  async function haalDefinitief(id, dk, rij, force) {
    var sig = JSON.stringify(rij), st = await lees(k(id, 'def:' + dk)), nu = Date.now();
    var recent = nu - grenzen(dk).eind < 7 * 86400000;
    if (st && st.v === 3 && st.sig === sig && (st.kw || (st.fout && !force)) && nu - st.at < (st.fout ? 86400000 : recent ? 6 * 3600000 : 30 * 86400000)) {
      if (st.fout) throw Error(st.fout + ' (eerdere controle)');
      return st.kw;
    }
    await wacht(BE_PAUZE);
    var j = await beGet('/customer/' + encodeURIComponent(id) + '/results-v2-api/', { interval: 'day', period: dk, include_vat: '0', include_delivery_cost: '0' });
    try { var kw = parseDefinitief(j, rij, dk); await schrijf(k(id, 'def:' + dk), { v: 3, sig: sig, at: nu, kw: kw }); return kw; }
    catch (e) { await schrijf(k(id, 'def:' + dk), { v: 3, sig: sig, at: nu, fout: e.message }); throw e; }
  }
  // BE's effectieve kwartierprijs is kosten / kWh: bij heel weinig afname of
  // teruglevering in dat kwartier ontspoort hij. Dan liever de marktprijs.
  function prijsBetrouwbaar(p, kwh) { return getal(p) && Math.abs(p) <= MAX_KAAL && kwh >= MIN_KWH; }
  function kiesPrijzen(kw, bron, prijzen, rij) {
    var gemIn = rij && getal(rij.avg_buy_price_eur_per_kwh) && Math.abs(rij.avg_buy_price_eur_per_kwh) <= MAX_KAAL ? rij.avg_buy_price_eur_per_kwh : null;
    var gemUit = rij && getal(rij.avg_sell_price_eur_per_kwh) && Math.abs(rij.avg_sell_price_eur_per_kwh) <= MAX_KAAL ? rij.avg_sell_price_eur_per_kwh : null;
    Object.keys(kw).forEach(function (q) {
      var x = kw[q], b = bron[q] || {}, a = prijzen[q], bronIn = 'BE', bronUit = 'BE';
      // 1. settlementprijs uit market_breakdown; 2. effectieve prijs (alleen bij genoeg volume).
      x.pIn = getal(b.mIn) && Math.abs(b.mIn) <= MAX_SETTLE ? b.mIn : prijsBetrouwbaar(b.pIn, b.imp) ? b.pIn : null;
      x.pUit = getal(b.mUit) && Math.abs(b.mUit) <= MAX_SETTLE ? b.mUit : prijsBetrouwbaar(b.pUit, b.exp) ? b.pUit : null;
      x.pInVast = getal(x.pIn); x.pUitVast = getal(x.pUit);
      if (!getal(x.pIn) && a && getal(a.ki) && Math.abs(a.ki) <= (a.vi ? MAX_SETTLE : MAX_KAAL)) { x.pIn = a.ki; x.pInVast = !!a.vi; bronIn = a.tnI ? 'TenneT' : a.allin ? 'markt/All-in' : 'markt'; if (a.v) x.prijsVoorlopig = true; }
      if (!getal(x.pUit) && a && getal(a.ke) && Math.abs(a.ke) <= (a.ve ? MAX_SETTLE : MAX_KAAL)) { x.pUit = a.ke; x.pUitVast = !!a.ve; bronUit = a.tnE ? 'TenneT' : a.allin ? 'markt/All-in' : 'markt'; if (a.v) x.prijsVoorlopig = true; }
      // Onbalans heeft buiten regeltoestand 2 één prijs voor tekort en overschot: zonder handel in de
      // ene richting is de onbalansprijs van de andere richting de beste benadering (niet vastgesteld).
      if (!getal(x.pIn) && getal(b.imbUit) && Math.abs(b.imbUit) <= MAX_SETTLE) { x.pIn = b.imbUit; x.pInVast = false; bronIn = 'onbalans (andere richting)'; }
      if (!getal(x.pUit) && getal(b.imbIn) && Math.abs(b.imbIn) <= MAX_SETTLE) { x.pUit = b.imbIn; x.pUitVast = false; bronUit = 'onbalans (andere richting)'; }
      if (!getal(x.pIn) && getal(gemIn)) { x.pIn = gemIn; bronIn = 'daggemiddelde'; x.prijsGeschatIn = true; }
      if (!getal(x.pUit) && getal(gemUit)) { x.pUit = gemUit; bronUit = 'daggemiddelde'; x.prijsGeschatUit = true; }
      x.prijsBron = bronIn === bronUit ? bronIn : 'in ' + bronIn + ' / uit ' + bronUit;
    });
    return kw;
  }
  async function dagDefinitief(id, dk, rij, c, force) {
    var bron = await haalDefinitief(id, dk, rij, force), d = verdeelDefinitief(bron, await cacheKwartieren(id, dk), ETA);
    // Ontlaadt de accu naar huis, dan is de afname ~0 en BE's effectieve prijs
    // onbruikbaar. De marktprijs van dat kwartier is dan de juiste waardering.
    if (dk >= plusDagen(vandaag(), -1)) { try { await herstelPrijzenDag(id, dk, 6 * 3600000); } catch (e) { if (e.pauze) throw e; noteer('Prijzen ' + dk + ': ' + e.message); } }
    // Kwartieren zonder accuwaarden bij BE (en zonder eigen metingen): parseDefinitief heeft al
    // gecontroleerd dat BE's dagtotaal ze ook niet meetelt; we volgen BE en tellen ze als 0.
    // Ze drukken de nauwkeurigheid (kwal) en staan in de notitie.
    var kw = kiesPrijzen(d.kw, bron, await prijzenDag(id, dk), rij), w = waardeer(kw, c, c.afrr ? afrrVan(rij) : null);
    var duur = (grenzen(dk).eind - grenzen(dk).start) / 1000;
    return { dk: dk, bron: 'BE', kw: kw, w: w, eta: ETA, dekking: Math.min(1, w.accuSec / duur), prijsDekking: Math.min(1, w.prijsSec / duur), uitCache: d.uitCache, geschat: d.geschat, aangevuld: d.aangevuld, ontbrekend: d.ontbrekend, kwal: kwaliteit(d, w) };
  }
  var maandCache = {};
  async function maandBron(id, ym, maxLeeftijd) {
    var m = maandCache[id + ym];
    if (m && Date.now() - m.at < maxLeeftijd) return m.j;
    var j = await beGet('/customer/' + encodeURIComponent(id) + '/results-v2-api/', { interval: 'month', period: ym, include_vat: '0', include_delivery_cost: '0' });
    if (!j || j.interval !== 'month' || j.period !== ym || !Array.isArray(j.series)) throw Error('Onverwacht maandantwoord voor ' + ym);
    maandCache[id + ym] = { at: Date.now(), j: j };
    return j;
  }
  function rijVan(j, dk) { return (j.series || []).find(function (r) { return r && r.date === dk; }) || null; }

  /* ── Payloads ── */
  function e2(n) { return (getal(n) ? (Math.abs(n) < 0.005 ? 0 : n) : 0).toFixed(2); }
  function k3(n) { return Math.max(0, getal(n) ? n : 0).toFixed(3); }
  function k4(n) { return Math.max(0, getal(n) ? n : 0).toFixed(4); }
  function intervallen(kw) {
    return Object.keys(kw).map(Number).sort(function (a, b) { return a - b; }).slice(0, 100).map(function (q) {
      var x = kw[q], o = { start: isoAms(q),
        batteryChargedKwh: k4(getal(x.geladen) ? x.geladen : x.ladNet + x.ladPV),
        batteryDischargedKwh: k4(getal(x.ontladen) ? x.ontladen : x.ontHuis + x.ontNet),
        gridImportedKwh: k4(x.imp), gridExportedKwh: k4(x.exp) };
      if (getal(x.soc)) o.batteryEndSoc = x.soc.toFixed(1);
      return o;
    });
  }
  /* ── Afstemming met de Home Assistant-integratie (gelijk aan calc.py) ──
   * Label achter elke dagnotitie: [BEMB v=… bron=ha|browser type=be|cache dekking=… actief=…]
   * 'actief' zet alleen Home Assistant (heartbeat, elke 15 min). */
  function maakLabel(bron, type, dekking, actief, kwal) {
    return '[BEMB v=' + VERSIE + ' bron=' + bron + ' type=' + type + ' dekking=' + (100 * dekking).toFixed(1) + (getal(kwal) ? ' kwal=' + (100 * kwal).toFixed(1) : '') + (actief ? ' actief=' + actief : '') + ']';
  }
  function leesLabel(note) {
    var m = typeof note === 'string' && /\[BEMB ([^\]]*)\]/.exec(note); if (!m) return null;
    var o = {}; m[1].split(/\s+/).forEach(function (x) { var i = x.indexOf('='); if (i > 0) o[x.slice(0, i)] = x.slice(i + 1); }); return o;
  }
  function magOverschrijven(bestaand, type, dekking, kwal) {
    var lab = leesLabel(bestaand && bestaand.note);
    if (!lab) return { mag: true, reden: '' };
    if (lab.type === 'be' && type !== 'be') return { mag: false, reden: 'definitieve versie staat er al' };
    if (lab.type === type) {
      // Zelfde soort: nauwkeurigheid (kwal) beslist — aandeel met eigen meting én vastgestelde prijs.
      // Labels zonder kwal (vóór 1.2.5) tellen als 0: die mogen altijd worden verbeterd.
      var oudK = Number(lab.kwal); if (!isFinite(oudK)) oudK = 0;
      if (oudK > 100 * (kwal || 0) + TOLERANTIE) return { mag: false, reden: 'bestaande versie is nauwkeuriger (' + oudK.toFixed(1) + '% tegen ' + (100 * (kwal || 0)).toFixed(1) + '%)' };
    }
    return { mag: true, reden: '' };
  }
  // Alleen bij aanvullen (HA actief): versturen als er niets staat of als onze versie duidelijk nauwkeuriger is.
  function nauwkeuriger(bestaand, r) {
    if (!bestaand) return true;
    var lab = leesLabel(bestaand.note), t = r.bron === 'BE' ? 'be' : 'cache';
    if (!lab) return true;
    if (lab.type === 'cache' && t === 'be') return true;
    if (lab.type === t) { var o = Number(lab.kwal); if (!isFinite(o)) o = 0; return 100 * (r.kwal || 0) > o + 1; }
    return false;
  }
  // Home Assistant actief? Jongste 'actief' in een label met bron=ha, max. 40 min oud.
  var HA_GRENS = 40 * 60000, haStatus = { at: 0, actief: null, dk: null, gecontroleerd: 0 };
  async function controleerHA(force) {
    if (!heeftSleutel || (!force && Date.now() - haStatus.gecontroleerd < 10 * 60000)) return haStatus;
    var tot = plusDagen(vandaag(), -1), van = plusDagen(vandaag(), -DAG_VENSTER), a = await mb('GET', '/api/results/daily?from=' + van + '&to=' + tot), j;
    try { j = JSON.parse(a.text); } catch (e) { j = {}; }
    var best = null, bestDk = null;
    ((j && j.results) || (Array.isArray(j) ? j : [])).forEach(function (r) {
      var lab = leesLabel(r && r.note), t = lab && lab.bron === 'ha' && lab.actief ? Date.parse(lab.actief) : NaN;
      if (isFinite(t) && (!best || t > best)) { best = t; bestDk = r.date; }
    });
    haStatus = { at: best || 0, actief: !!best && Date.now() - best < HA_GRENS, dk: bestDk, gecontroleerd: Date.now() };
    return haStatus;
  }
  function haNeemtOver(c) { return c.afstemming !== 'altijd' && haStatus.actief; }
  // Hartslag van de browser: verstuurt de plugin (HA kon zijn heartbeat niet kwijt), dan zet hij
  // elke 10 minuten actief=<tijd> in zijn eigen recentste dagrecord, met dezelfde waarden.
  // Home Assistant stuurt dan geen live; is de hartslag ouder dan 25 min (dashboard dicht), wel.
  var laatsteHartslag = 0;
  function zetActief(note, iso) {
    return String(note || '').replace(/\[BEMB ([^\]]*)\]/, function (m, inner) { return '[BEMB ' + inner.replace(/\s*actief=\S+/, '') + ' actief=' + iso + ']'; });
  }
  async function browserHartslag(id, c) {
    if (c.proef || Date.now() - laatsteHartslag < 10 * 60000) return;
    laatsteHartslag = Date.now();
    var tot = plusDagen(vandaag(), -1), van = plusDagen(vandaag(), -DAG_VENSTER), a = await mb('GET', '/api/results/daily?from=' + van + '&to=' + tot), j;
    try { j = JSON.parse(a.text); } catch (e) { j = {}; }
    var recs = ((j && j.results) || []).filter(function (r) { return r && r.date; }).sort(function (x, y) { return x.date < y.date ? 1 : -1; });
    for (var r of recs) {
      var lab = leesLabel(r.note);
      if (!lab || lab.bron !== 'browser') continue;
      var dk = String(r.date).slice(0, 10), st = await lees(k(id, 'sent:' + dk));
      if (!st || !st.payload || st.payload.test) continue;
      await mb('POST', '/api/results/daily', Object.assign({}, st.payload, { note: zetActief(st.payload.note, isoAms(Date.now())) }));
      return dk;
    }
  }
  // Zon en gemiddelde prijzen; alleen als ze volledig bekend zijn (anders liever weglaten dan fout).
  function extraVelden(w, p) {
    if (!w.pvOnbekend && w.kwartieren) { p.solarKwhGenerated = k3(w.pv); p.solarKwhConsumed = k3(Math.max(w.pv - w.pvExport, 0)); }
    if (w.imp > 0.01 && w.impMetPrijs >= 0.99 * w.imp) p.avgBuyPrice = (w.impKost / w.impMetPrijs).toFixed(4);
    if (w.exp > 0.01 && w.expMetPrijs >= 0.99 * w.exp) p.avgSellPrice = (w.expOpbr / w.expMetPrijs).toFixed(4);
    return p;
  }
  function dagPayload(r, c) {
    // Meer dan 2 uur met daggemiddelde prijs: geen kwartierwaardering meer, dus als onzeker markeren.
    var w = r.w, invalid = r.bron === 'cache' ? (r.dekking < 0.99 || r.prijsDekking < 0.99 || w.zonderPrijs > 0) : (w.zonderPrijs > 0 || (w.geschattePrijs || 0) > 8 || (r.ontbrekend || 0) > 4);
    var note = (r.bron === 'BE' ? 'BE definitief' + (r.aangevuld ? ', ' + r.aangevuld + ' kwartieren accuwaarden uit eigen metingen' : '') +
      (r.ontbrekend ? ', ' + r.ontbrekend + ' kwartieren zonder accudata bij BE (als 0, zoals BE\'s dagtotaal)' : '') : 'Voorlopig uit browsercache, dekking ' + (100 * r.dekking).toFixed(1) + '%') +
      (w.geschattePrijs ? '. ' + w.geschattePrijs + ' kwartieren met daggemiddelde prijs' : '') + '. All-in' + (c.saldeert ? ', EB op teruglevering t/m 2026' : ', zonder EB op teruglevering') +
      '. Besparing = ontladen naar huis minus evenredige laadkosten; handel = ontladen naar net minus evenredige laadkosten' +
      (r.bron === 'BE' && c.afrr ? ' plus aFRR-vergoeding' : '') + '. ' + etaTekst(r.eta || ETA) + '.';
    var label = maakLabel('browser', r.bron === 'BE' ? 'be' : 'cache', r.dekking, null, r.kwal);
    note = note.slice(0, 480 - label.length - 1) + ' ' + label;
    return extraVelden(w, { date: r.dk, batteryResult: e2(w.totaal), batterySavings: e2(w.besparing),
      chargedToday: k3(w.geladen), dischargedToday: k3(w.ontladen), invalid: !!invalid, note: note, intervals: intervallen(r.kw) });
  }

  /* ── Rondes ── */
  function lock(naam, fn) {
    if (navigator.locks && navigator.locks.request) return navigator.locks.request(naam, { ifAvailable: true }, function (l) { return l ? fn() : null; });
    return fn();
  }
  async function verzamel() {
    var id = klant(); if (!id || document.hidden) return;
    return lock('bemb-verzamel', async function () {
      try { await verzamelPrijzen(id); } catch (e) { noteer('Prijzen: ' + e.message); }
      try { await verzamelRauw(id); } catch (e) { noteer('Metingen: ' + e.message); }
      laatsteVerzameling = Date.now();
    });
  }
  var laatsteVerzameling = 0, laatsteLive = null, laatsteDagronde = 0;
  async function liveRonde(handmatig) {
    var id = klant(), c = cfg(); if (!id) throw Error('Geen installatie bekend');
    if (!handmatig && (!c.auto || !heeftSleutel)) return;
    try { await controleerHA(false); } catch (e) {}
    if (haNeemtOver(c)) throw Error('Home Assistant verstuurt (actief ' + klok(haStatus.at) + ').');
    return lock('bemb-live', async function () {
      var nu = Date.now(), dk = dagVan(nu);
      var rows = ((await lees(k(id, 'rauw:' + dk))) || {}).rows || [], m = null;
      for (var i = rows.length - 1; i >= 0; i--) if (getal(rows[i].bo)) { m = rows[i]; break; }
      if (!m || nu - m.t > 300000) throw Error('Geen actuele accumeting (jonger dan 5 min); live niet verstuurd');
      var r = await dagUitCache(id, dk, m.t, c), w = r.w;
      var p = { timestamp: isoAms(m.t), batteryResult: e2(w.totaal), batterySavings: e2(w.besparing),
        chargedToday: k3(w.geladen), dischargedToday: k3(w.ontladen), gridImportToday: k3(w.imp), gridExportToday: k3(w.exp),
        invalid: r.dekking < 0.99 || w.zonderPrijs > 0 };
      extraVelden(w, p);
      if (getal(m.s) && m.s >= 0 && m.s <= 100) p.batteryCharge = m.s.toFixed(1);
      if (c.proef) p.test = true;
      await mb('POST', '/api/live', p);
      laatsteLive = { at: Date.now(), p: p };
      try { await browserHartslag(id, c); } catch (e) { noteer('Hartslag: ' + e.message); }
      noteer('Live ' + (c.proef ? 'gevalideerd (proef)' : 'verstuurd') + ': totaal € ' + p.batteryResult + ', besparing € ' + p.batterySavings + (p.invalid ? ' · onvolledig' : ''));
      render();
    });
  }
  // Eén dag bepalen: BE definitief als de dag complete is, anders de cache.
  async function bepaalDag(id, dk, maandJ, c, force, rapport) {
    var rij = maandJ ? rijVan(maandJ, dk) : null;
    if (rij && rij.status === 'complete') {
      try { return await dagDefinitief(id, dk, rij, c, force); }
      catch (e) { if (e.pauze) throw e; rapport.push(dk + ': BE complete maar niet bruikbaar (' + e.message + '); cache gebruikt.'); }
    }
    var r = await dagUitCache(id, dk, null, c);
    return r.dekking >= MIN_DEKKING_VOORLOPIG ? r : null;
  }
  async function verstuurDag(id, r, c, rapport, bestaand, uitkomst) {
    uitkomst = uitkomst || {};
    if (bestaand !== undefined) {
      var regel = magOverschrijven(bestaand, r.bron === 'BE' ? 'be' : 'cache', r.dekking, r.kwal);
      if (!regel.mag) {
        uitkomst.wat = 'geweigerd'; uitkomst.reden = regel.reden + (((leesLabel(bestaand && bestaand.note) || {}).bron === 'ha') ? ' (Home Assistant)' : '');
        rapport.push(r.dk + ': niet vervangen — ' + uitkomst.reden + '.'); return false;
      }
    }
    var p = dagPayload(r, c), hash = JSON.stringify(p), sk = k(id, (c.proef ? 'proef:' : 'sent:') + r.dk), st = await lees(sk);
    if (st && st.bron === 'BE' && r.bron !== 'BE') { uitkomst.wat = 'geweigerd'; uitkomst.reden = 'definitieve versie al verstuurd'; rapport.push(r.dk + ': definitieve versie al verstuurd; cache niet teruggezet.'); return false; }
    // Hash alleen vertrouwen als de huidige versie op Mijnbatterij van de browser zelf is.
    var vanAnder = bestaand && ((leesLabel(bestaand.note) || {}).bron || '') !== 'browser';
    if (st && st.hash === hash && !vanAnder) { uitkomst.wat = 'ongewijzigd'; return false; }
    uitkomst.wat = 'verstuurd';
    var body = c.proef ? Object.assign({ test: true }, p) : p;
    await mb('POST', '/api/results/daily', body);
    await schrijf(sk, { hash: hash, bron: r.bron, at: Date.now(), totaal: p.batteryResult, besparing: p.batterySavings, payload: body });
    rapport.push(r.dk + ': ' + (c.proef ? 'gevalideerd (proef)' : 'verstuurd') + ' · ' + r.bron + ' · € ' + p.batteryResult + ' (besparing € ' + p.batterySavings + ')' + (p.invalid ? ' · onvolledig' : ''));
    return true;
  }
  async function dagRonde(handmatig) {
    var id = klant(), c = cfg(); if (!id) throw Error('Geen installatie bekend');
    if (!handmatig && (!c.auto || !heeftSleutel)) return;
    try { await controleerHA(handmatig); } catch (e) { noteer('HA-controle: ' + e.message); }
    // Verstuurt Home Assistant, dan vult de browser alleen aan: dagen die er nog niet staan,
    // of die hij nauwkeuriger heeft (bijv. van vóór de HA-installatie, met meer eigen metingen).
    var aanvullen = haNeemtOver(c);
    return lock('bemb-dagen', async function () {
      var rapport = [], vandaagK = vandaag(), dagen = [];
      for (var i = DAG_VENSTER; i >= 1; i--) dagen.push(plusDagen(vandaagK, -i));
      var bestaand = null;
      try { var ba = await mb('GET', '/api/results/daily?from=' + dagen[0] + '&to=' + dagen[dagen.length - 1]), bj = JSON.parse(ba.text); bestaand = {};
        ((bj && bj.results) || []).forEach(function (x) { if (x && x.date) bestaand[String(x.date).slice(0, 10)] = x; }); }
      catch (e) { rapport.push('Mijnbatterij-records niet opgehaald (' + e.message + '); geen dagen verstuurd.'); return rapport.join('\n'); }
      var maandJ = {}, overgelaten = 0;
      for (var ym of Array.from(new Set(dagen.map(function (d) { return d.slice(0, 7); })))) {
        try { maandJ[ym] = await maandBron(id, ym, handmatig ? 60000 : 2 * 3600000); }
        catch (e) { if (e.pauze) { rapport.push(e.message); break; } rapport.push(ym + ': ' + e.message); }
      }
      for (var dk of dagen) {
        try {
          var r = await bepaalDag(id, dk, maandJ[dk.slice(0, 7)], c, handmatig, rapport);
          if (!r) continue;
          if (aanvullen && !nauwkeuriger(bestaand[dk] || null, r)) { overgelaten++; continue; }
          await verstuurDag(id, r, c, rapport, bestaand[dk] || null);
        } catch (e) { rapport.push(dk + ': ' + e.message); if (e.pauze) break; }
      }
      laatsteDagronde = Date.now();
      if (aanvullen) rapport.push('Dagen: Home Assistant verstuurt (actief ' + klok(haStatus.at) + ')' + (overgelaten ? '; ' + overgelaten + ' dagen aan HA gelaten' : '') + '.');
      if (!rapport.length) rapport.push('Dagen: niets gewijzigd.');
      rapport.forEach(noteer); render();
      return rapport.join('\n');
    });
  }
  /* ── Historie per maand ──
     Maanden aanvinken en versturen; per dag een regel in de stroom; per maand een status die
     bewaard blijft (k(id,'hist:'+ym)):
       ok      alle complete dagen verwerkt en (afgelopen maand) maandtotaal verstuurd/ongewijzigd
       lopend  huidige maand: alle complete dagen tot nu verwerkt (maandtotaal pas na afloop)
       deels   één of meer dagen niet bruikbaar of nog niet complete; maandtotaal niet vervangen
       fout    Mijnbatterij/BE-fout of gestopt
       leeg    geen BE-dagen in die maand (vóór de batterij)
       proef   proefstand: gevalideerd, niets opgeslagen */
  var historieStop = false, historieBezig = false, histVoortgang = null;
  function histStroom(regel) {
    var t = new Date().toLocaleTimeString('nl-NL', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    histStroomRegels.push(t + '  ' + regel); if (histStroomRegels.length > 500) histStroomRegels.splice(0, histStroomRegels.length - 500);
    if (shadow) { var el = $('hist-stroom'); if (el) { el.hidden = false; el.textContent = histStroomRegels.join('\n'); el.scrollTop = el.scrollHeight; } }
  }
  var histStroomRegels = [];
  function eur(x) { return '€ ' + (Math.round(x * 100) / 100).toFixed(2).replace('.', ','); }
  // Mini-tabel per maand: de TenneT-settlementprijzen waarmee gerekend wordt, met een link naar TenneT per dag.
  async function toonTennet(ym, dagen) {
    if (!shadow || !$('hist-tennet')) return;
    var e3 = function (v) { return getal(v) ? v.toFixed(3).replace('.', ',') : '—'; };
    var rijen = [];
    for (var i = 0; i < dagen.length; i++) {
      var st = await lees('nl:tennet:' + dagen[i]), rows = (st && st.rows) || {}, n = (grenzen(dagen[i]).eind - grenzen(dagen[i]).start) / Q;
      var ki = [], ke = [];
      Object.keys(rows).forEach(function (q) { if (getal(rows[q].ki)) ki.push(rows[q].ki); if (getal(rows[q].ke)) ke.push(rows[q].ke); });
      var gem = function (a) { return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; };
      var dmy = dagen[i].slice(8, 10) + '-' + dagen[i].slice(5, 7) + '-' + dagen[i].slice(0, 4);
      rijen.push('<tr><td>' + dagen[i] + '</td><td>' + Object.keys(rows).length + '/' + n + (st && st.compleet ? ' ✓' : '') + '</td><td>' + e3(gem(ki)) + '</td><td>' +
        e3(ki.length ? Math.min.apply(null, ki) : null) + ' – ' + e3(ki.length ? Math.max.apply(null, ki) : null) + '</td><td>' + e3(gem(ke)) + '</td><td>' +
        e3(ke.length ? Math.min.apply(null, ke) : null) + ' – ' + e3(ke.length ? Math.max.apply(null, ke) : null) + '</td><td><a href="' +
        esc(TENNET_PAGINA + 'f?p=100:90035:::::P90035_PTU_DATE_LOC:' + dmy + '&p_lang=en') + '" target="_blank" rel="noopener">TenneT</a></td></tr>');
    }
    var oud = $('hist-tn-' + ym); if (oud) oud.remove();
    var el = document.createElement('details'); el.id = 'hist-tn-' + ym; el.className = 'tn';
    el.innerHTML = '<summary>' + esc(ym) + ' — TenneT-settlementprijzen waarmee gerekend wordt (' + dagen.length + ' dagen, €/kWh)</summary>' +
      '<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Kwartieren</th><th>Tekort gem.</th><th>Tekort min – max</th><th>Overschot gem.</th><th>Overschot min – max</th><th>Bron</th></tr></thead><tbody>' +
      rijen.join('') + '</tbody></table></div>';
    $('hist-tennet').hidden = false; $('hist-tennet').appendChild(el);
  }
  async function verstuurMaand(id, ym, c, vk, rapport) {
    var res = { ym: ym, at: Date.now(), st: 'fout', dagen: 0, n: 0, verstuurd: 0, ongewijzigd: 0, geweigerd: 0, overgeslagen: 0, nietComplete: 0, totaal: 0, besparing: 0, maand: '', proef: !!c.proef, versie: VERSIE };
    var j; try { await wacht(BE_PAUZE); j = await maandBron(id, ym, 60000); }
    catch (e) { res.fout = e.message; histStroom(ym + '  BE-maand niet opgehaald: ' + e.message); if (e.pauze) historieStop = true; return res; }
    var dagen = (j.series || []).map(function (r) { return r && r.date; }).filter(function (d) { return typeof d === 'string' && d.slice(0, 7) === ym && d < vk; }).sort();
    res.dagen = dagen.length;
    if (!dagen.length) { res.st = 'leeg'; histStroom(ym + '  geen BE-dagen in deze maand.'); return res; }
    // Bestaande records ophalen: ook de historie volgt de afstemmingsregel (niets nauwkeurigers overschrijven).
    var bestaand = {};
    try { var ba = await mb('GET', '/api/results/daily?from=' + dagen[0] + '&to=' + dagen[dagen.length - 1]), bj = JSON.parse(ba.text);
      ((bj && bj.results) || []).forEach(function (x) { if (x && x.date) bestaand[String(x.date).slice(0, 10)] = x; }); }
    catch (e) { res.fout = 'Mijnbatterij-records niet opgehaald: ' + e.message; histStroom(ym + '  ' + res.fout); return res; }
    // Eerst controleren dat TenneT's settlementprijzen voor elke dag binnen zijn: zonder die prijzen
    // rekent de plugin met benaderingen en zou hij verkeerde bedragen versturen. Dan niets versturen.
    var tnWacht = {}, tnMist = [];
    histStroom(ym + '  TenneT-prijzen controleren…');
    for (var ti = 0; ti < dagen.length; ti++) {
      if (historieStop) break;
      var ts = await tennetStatus(dagen[ti]);
      if (ts.ok) continue;
      // TenneT publiceert een dag pas na afloop; de laatste dagen kunnen nog ontbreken (volgt vanzelf).
      if (dagen[ti] >= plusDagen(vk, -2)) tnWacht[dagen[ti]] = ts.fout;
      else tnMist.push(dagen[ti] + ' (' + ts.fout + ')');
    }
    if (historieStop) { res.fout = 'gestopt'; histStroom(ym + '  gestopt.'); return res; }
    if (tnMist.length) {
      res.fout = 'TenneT-prijzen ontbreken voor ' + tnMist.length + ' dag(en)'; res.tennet = 'mist';
      // Geen enkele dag gelukt: TenneT is niet bereikbaar; dan ook de volgende maanden niet proberen.
      if (tnMist.length === dagen.length - Object.keys(tnWacht).length) historieStop = true;
      histStroom(ym + '  NIET verstuurd — TenneT-prijzen ontbreken voor: ' + tnMist.join(', ') + '. Probeer het later opnieuw.');
      return res;
    }
    histStroom(ym + '  TenneT-prijzen compleet voor ' + (dagen.length - Object.keys(tnWacht).length) + '/' + dagen.length + ' dagen' +
      (Object.keys(tnWacht).length ? ' (' + Object.keys(tnWacht).join(', ') + ' nog niet gepubliceerd — volgt)' : '') + '.');
    try { await toonTennet(ym, dagen.filter(function (d) { return !tnWacht[d]; })); } catch (e) {}
    var som = { t: 0, b: 0, l: 0, o: 0, n: 0 }, alles = true, gestopt = false;
    for (var di = 0; di < dagen.length; di++) {
      var dk = dagen[di];
      if (historieStop) { gestopt = true; break; }
      if (histVoortgang) { histVoortgang.dag = di + 1; histVoortgang.dagen = dagen.length; toonVoortgang(); }
      if (tnWacht[dk]) { res.wachtOpBE = (res.wachtOpBE || 0) + 1; histStroom(dk + '  TenneT-prijzen nog niet gepubliceerd (volgt vanzelf)'); continue; }
      var rij = rijVan(j, dk);
      if (!rij || rij.status !== 'complete') {
        // BE maakt een dag pas na ~48 uur definitief: de laatste dagen zijn nog niet klaar (verwacht).
        if (dk >= plusDagen(vk, -3)) { res.wachtOpBE = (res.wachtOpBE || 0) + 1; histStroom(dk + '  nog niet definitief bij BE (volgt vanzelf)'); }
        else { alles = false; res.nietComplete++; histStroom(dk + '  niet complete bij BE — overgeslagen'); }
        continue;
      }
      try {
        var r = await dagDefinitief(id, dk, rij, c, false), u = {};
        await verstuurDag(id, r, c, rapport, bestaand[dk] || null, u);
        som.t += r.w.totaal; som.b += r.w.besparing; som.l += r.w.geladen; som.o += r.w.ontladen; som.n++;
        res[u.wat] = (res[u.wat] || 0) + 1;
        histStroom(dk + '  ' + eur(r.w.totaal) + '  (besparing ' + eur(r.w.besparing) + ', handel ' + eur(r.w.totaal - r.w.besparing) + ')  ' +
          r.w.geladen.toFixed(1) + '↑ ' + r.w.ontladen.toFixed(1) + '↓ kWh  kwal ' + Math.round(100 * (r.kwal || 0)) + '%  TenneT-prijs in ' +
          Object.keys(r.kw).filter(function (q) { return /TenneT/.test(r.kw[q].prijsBron || ''); }).length + ' kwartieren  → ' +
          (u.wat === 'verstuurd' ? (c.proef ? 'gevalideerd (proef)' : 'verstuurd') : u.wat === 'ongewijzigd' ? 'ongewijzigd' : 'niet vervangen: ' + u.reden));
      } catch (e) {
        alles = false; res.overgeslagen++; histStroom(dk + '  overgeslagen: ' + e.message);
        if (e.pauze) { historieStop = true; gestopt = true; break; }
      }
    }
    res.n = som.n; res.totaal = som.t; res.besparing = som.b;
    var afgelopen = ym < vk.slice(0, 7);
    if (gestopt) { res.st = 'fout'; res.fout = 'gestopt'; histStroom(ym + '  gestopt na ' + som.n + '/' + dagen.length + ' dagen.'); return res; }
    var nogBE = res.wachtOpBE || 0;
    if (afgelopen && alles && som.n === dagen.length) {
      var mp = { yearMonth: ym, batteryResult: e2(som.t), batterySavings: e2(som.b), batteryCharged: k3(som.l), batteryDischarged: k3(som.o),
        partial: som.n < dagenInMaand(ym), note: 'Som van ' + som.n + ' BE-definitieve dagen. BE-Mijnbatterij ' + VERSIE + '.' };
      var mk = k(id, 'maand:' + ym), oud = await lees(mk), hash = JSON.stringify(mp);
      if (c.proef) { res.maand = 'proef'; res.st = 'proef'; }
      else if (!oud || oud.hash !== hash) {
        try { await mb('POST', '/api/results/monthly', mp); await schrijf(mk, { hash: hash, at: Date.now() }); res.maand = 'verstuurd'; res.st = 'ok'; }
        catch (e) { res.maand = 'mislukt'; res.fout = 'maandtotaal: ' + e.message; res.st = 'fout'; }
      } else { res.maand = 'ongewijzigd'; res.st = 'ok'; }
      histStroom(ym + '  ' + som.n + '/' + dagen.length + ' dagen · maandtotaal ' + eur(som.t) + ' (besparing ' + eur(som.b) + ') ' +
        ({ verstuurd: 'verstuurd ✓', ongewijzigd: 'ongewijzigd ✓', proef: '— proefstand, niet verstuurd', mislukt: 'MISLUKT: ' + res.fout })[res.maand]);
    } else if (alles && som.n + nogBE === dagen.length) {
      // Lopende maand, of een net afgelopen maand waarvan BE de laatste dagen nog niet definitief heeft.
      res.st = c.proef ? 'proef' : 'lopend';
      histStroom(ym + '  ' + som.n + ' dagen verwerkt' + (nogBE ? ', ' + nogBE + ' nog niet definitief bij BE of TenneT' : '') + ' — maandtotaal volgt als de maand compleet is.');
    } else {
      res.st = 'deels';
      histStroom(ym + '  ' + som.n + '/' + dagen.length + ' dagen verwerkt; maandtotaal niet vervangen' + (res.overgeslagen ? ' (' + res.overgeslagen + ' niet bruikbaar)' : '') + '.');
    }
    return res;
  }
  async function verstuurMaanden(lijst) {
    var id = klant(), c = cfg(); if (!id) throw Error('Geen installatie bekend');
    lijst = Array.from(new Set(lijst)).filter(function (ym) { return /^\d{4}-\d{2}$/.test(ym); }).sort();
    if (!lijst.length) throw Error('Vink eerst één of meer maanden aan');
    if (historieBezig) throw Error('Historie loopt al');
    historieBezig = true; historieStop = false; BE_PAUZE = HISTORIE_PAUZE;
    var rapport = [], uitslag = [];
    histStroom('Start: ' + lijst.join(', ') + (c.proef ? ' (proefstand)' : '') + ' — ca. 4 s per dag.');
    try {
      var gestart = false;
      await lock('bemb-dagen', async function () {
        gestart = true;
        var vk = vandaag();
        for (var mi = 0; mi < lijst.length; mi++) {
          if (historieStop) { histStroom('Gestopt door gebruiker.'); break; }
          var ym = lijst[mi];
          histVoortgang = { ym: ym, maand: mi + 1, maanden: lijst.length, dag: 0, dagen: 0 }; toonVoortgang();
          histStatus[ym] = Object.assign({}, histStatus[ym] || {}, { bezig: true }); tekenMaanden();
          var res = await verstuurMaand(id, ym, c, vk, rapport);
          histStatus[ym] = res; await schrijf(k(id, 'hist:' + ym), res);
          if (res.st !== 'fout' || res.fout !== 'gestopt') histSelectie.delete(ym);
          uitslag.push(ym + ': ' + res.st); tekenMaanden();
        }
      });
      if (!gestart) throw Error('Er loopt al een dagronde; probeer het zo opnieuw.');
    } finally {
      historieBezig = false; BE_PAUZE = 1250; histVoortgang = null; toonVoortgang();
      Object.keys(histStatus).forEach(function (ym) { if (histStatus[ym] && histStatus[ym].bezig) delete histStatus[ym].bezig; });
      tekenMaanden();
    }
    histStroom('Klaar: ' + uitslag.join(' · '));
    rapport.forEach(noteer); render();
    return uitslag.join(' · ');
  }
  // Oude aanroep (vanaf een maand): alle maanden t/m nu.
  async function historie(vanYm) { return verstuurMaanden(maanden(vanYm, vandaag().slice(0, 7))); }
  var histStatus = {}, histSelectie = new Set(), histJaar = null;
  async function laadHistStatus() {
    var id = klant(); if (!id) return;
    var nu = vandaag(), j0 = +nu.slice(0, 4) - 4;
    for (var y = j0; y <= +nu.slice(0, 4); y++) for (var m = 1; m <= 12; m++) {
      var ym = y + '-' + String(m).padStart(2, '0'); if (ym > nu.slice(0, 7)) break;
      if (histStatus[ym] && histStatus[ym].bezig) continue;
      var v = await lees(k(id, 'hist:' + ym)); if (v) histStatus[ym] = v; else delete histStatus[ym];
    }
  }
  var MAAND_KORT = ['jan', 'feb', 'mrt', 'apr', 'mei', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
  var HIST_TEKEN = { ok: '✓', lopend: '✓', deels: '!', fout: '✕', leeg: '–', proef: '○' };
  var HIST_UITLEG = { ok: 'gelukt', lopend: 'bijgewerkt; maandtotaal volgt als de maand compleet is', deels: 'deels: niet alle dagen bruikbaar', fout: 'mislukt', leeg: 'geen BE-dagen', proef: 'proefstand: gevalideerd, niet opgeslagen' };
  function tekenMaanden() {
    if (!shadow || !$('hist-grid')) return;
    var nu = vandaag().slice(0, 7), jaar = histJaar || +nu.slice(0, 4);
    $('hist-jaar').textContent = jaar;
    $('hist-vorig').disabled = jaar <= +nu.slice(0, 4) - 4; $('hist-volgend').disabled = jaar >= +nu.slice(0, 4);
    var html = '';
    for (var m = 1; m <= 12; m++) {
      var ym = jaar + '-' + String(m).padStart(2, '0'), st = histStatus[ym] || null, toekomst = ym > nu;
      var cls = 'mnd' + (histSelectie.has(ym) ? ' sel' : '') + (st && st.bezig ? ' bezig' : st ? ' st-' + st.st : '');
      var titel = toekomst ? 'nog niet' : st && st.bezig ? 'bezig…' : st ? HIST_UITLEG[st.st] + ' · ' + new Date(st.at).toLocaleString('nl-NL', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) +
        (st.n ? ' · ' + st.n + '/' + st.dagen + ' dagen · ' + eur(st.totaal) : '') + (st.fout ? ' · ' + st.fout : '') : 'nog niet verstuurd';
      html += '<button type="button" class="' + cls + '" data-ym="' + ym + '"' + (toekomst || historieBezig ? ' disabled' : '') + ' aria-pressed="' + histSelectie.has(ym) + '" title="' + titel.replace(/"/g, '&quot;') + '">' +
        '<span class="vink" aria-hidden="true"></span><span class="naam">' + MAAND_KORT[m - 1] + '</span>' +
        '<span class="teken" aria-label="' + (st && !st.bezig ? HIST_UITLEG[st.st] : '') + '">' + (st && st.bezig ? '…' : st ? HIST_TEKEN[st.st] || '' : '') + '</span>' +
        (st && !st.bezig && st.n ? '<small>' + eur(st.totaal) + '</small>' : '<small>&nbsp;</small>') + '</button>';
    }
    $('hist-grid').innerHTML = html;
    var n = histSelectie.size;
    $('hist-go').textContent = n ? 'Verstuur ' + n + (n === 1 ? ' maand' : ' maanden') : 'Verstuur';
    $('hist-go').disabled = !n || historieBezig; $('hist-stop').hidden = !historieBezig;
    $('hist-alles').disabled = $('hist-niets').disabled = historieBezig;
  }
  function toonVoortgang() {
    if (!shadow || !$('hist-voortgang')) return;
    var v = histVoortgang, bar = $('hist-balk');
    $('hist-voortgang').hidden = !v;
    if (!v) return;
    var frac = ((v.maand - 1) + (v.dagen ? v.dag / v.dagen : 0)) / v.maanden;
    bar.style.width = Math.round(100 * frac) + '%';
    $('hist-voortgang-tekst').textContent = 'Maand ' + v.maand + '/' + v.maanden + ' (' + v.ym + ')' + (v.dagen ? ' · dag ' + v.dag + '/' + v.dagen : '');
  }

  /* ── Planner ── */
  async function tik() {
    try {
      if (document.hidden) return;
      await verzamel();
      var c = cfg();
      try { await controleerHA(false); } catch (e) {}
      if (c.auto && heeftSleutel) {
        try { await liveRonde(false); } catch (e) { noteer(e.message); }
        if (Date.now() - laatsteDagronde > 30 * 60000) { try { await dagRonde(false); } catch (e) { noteer(e.message); } }
      }
      if (opened) render();
    } catch (e) { noteer(e.message); }
  }

  /* ── Pagina in het menu ── */
  var root = null, shadow = null, opened = false, navLinks = [], activeBefore = [], menuSerial = 0;
  function $(id) { return shadow.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function euro(n) { return getal(n) ? new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR' }).format(Math.abs(n) < 0.005 ? 0 : n) : '—'; }
  function kwhT(n) { return getal(n) ? n.toLocaleString('nl-NL', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + ' kWh' : '—'; }
  function pct(n) { return getal(n) ? (100 * n).toLocaleString('nl-NL', { maximumFractionDigits: 1 }) + '%' : '—'; }
  function cls(n) { return getal(n) ? (n >= 0 ? 'pos' : 'neg') : ''; }
  function status(s) { if (shadow) $('status').textContent = s; }
  function donker() { return document.documentElement.getAttribute('data-be-dark') === 'aan'; }
  function thema() {
    if (!root) return;
    if (donker()) root.setAttribute('data-dark', ''); else root.removeAttribute('data-dark');
    var stijl = window.BETheme && typeof window.BETheme.style === 'function' ? window.BETheme.style() : '';
    if (stijl) root.setAttribute('data-dark-style', stijl); else root.removeAttribute('data-dark-style');
  }
  function knop(label, attr) { return '<button type="button" ' + (attr || '') + '>' + label + '</button>'; }
  function schakel(id, titel, uitleg) {
    return '<label class="switch"><input type="checkbox" id="' + id + '"><span class="track" aria-hidden="true"><span></span></span><span><strong>' + titel + '</strong><small>' + uitleg + '</small></span></label>';
  }
  function mount() {
    if (root) return;
    root = document.createElement('section'); root.id = 'bemb-root'; root.hidden = true;
    root.style.cssText = 'position:fixed;left:0;right:0;top:0;bottom:0;z-index:1039;overflow:auto;overscroll-behavior:contain';
    shadow = root.attachShadow({ mode: 'open' });
    shadow.innerHTML = '<style>' +
      ':host{--bg:#f6f7fb;--paper:#fff;--ink:#292633;--muted:#74717e;--line:#e7e4ec;--purple:#714291;--soft:#f1edf6;--green:#208557;--red:#c64151;--button:#714291;color-scheme:light;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink)}' +
      ':host([data-dark]){--bg:#282723;--paper:#36342f;--ink:#f5f0e8;--muted:#c0b8ab;--line:#575148;--purple:#cfb3f0;--soft:#44384f;--green:#62d0ad;--red:#ff96a8;--button:#8057a4;color-scheme:dark}' +
      ':host([data-dark][data-dark-style="grafiet"]){--bg:#111316;--paper:#1c1f26;--ink:#f5f7fa;--muted:#b6c0cf;--line:#3c4351;--purple:#a879ff;--soft:#262a34;--green:#23d9a0;--red:#ff6079;--button:#7340b5}' +
      '*{box-sizing:border-box}[hidden]{display:none!important}h1,h2,p{margin:0}h1{font-size:23px}h2{font-size:16px;margin-bottom:10px}small,.muted{color:var(--muted)}' +
      'button,input,select{font:inherit;color:var(--ink)}button,input[type=month],input[type=password]{border:1px solid var(--line);border-radius:7px;background:var(--paper);padding:8px 12px;min-height:38px}button{cursor:pointer}button:hover{border-color:var(--purple)}button:disabled{opacity:.5;cursor:default}button.primary{background:var(--button);border-color:var(--button);color:#fff;font-weight:600}' +
      'button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--purple);outline-offset:2px}' +
      '.page{padding:24px 28px 40px;background:var(--bg);min-height:100%}.wrap{max-width:1100px;margin:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}' +
      '.pill{display:inline-block;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;background:var(--soft);color:var(--purple);margin-left:6px}.pill.warn{color:var(--red)}' +
      '.panel{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:18px;margin-top:14px}' +
      '.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.tile{background:var(--soft);border-radius:8px;padding:12px 14px}.tile small{display:block;font-size:11px}.tile strong{font-size:22px;font-variant-numeric:tabular-nums}.tile .sub{font-size:11px;color:var(--muted);margin-top:2px}' +
      '.pos{color:var(--green)}.neg{color:var(--red)}' +
      '.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}th,td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{font-size:10px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);font-weight:600}' +
      'tr.dag{cursor:pointer}tr.dag:hover{background:var(--soft)}tr.detail td{text-align:left;white-space:normal;background:var(--soft)}.ok{color:var(--green)}.afw{color:var(--red);font-weight:600}' +
      '.tag{font-size:11px;padding:2px 7px;border-radius:5px;background:var(--soft)}.tag.be{color:var(--green)}.tag.cache{color:var(--purple)}' +
      '.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.row input[type=password]{flex:1;min-width:220px}' +
      '.switch{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer}.switch input{position:absolute;opacity:0;pointer-events:none}.switch small{display:block;font-size:12px}' +
      '.track{flex:none;position:relative;width:44px;height:24px;border-radius:12px;background:var(--line);transition:background .2s;margin-top:2px}.track span{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 3px #0003;transition:transform .2s}' +
      '.switch input:checked+.track{background:var(--button)}.switch input:checked+.track span{transform:translateX(20px)}.switch input:focus-visible+.track{outline:2px solid var(--purple);outline-offset:2px}' +
      '.notice{font-size:12px;color:var(--muted);margin-top:10px;min-height:18px}pre{white-space:pre-wrap;font:11px/1.55 ui-monospace,Menlo,monospace;background:var(--soft);border-radius:8px;padding:10px;max-height:260px;overflow:auto;margin:10px 0 0}' +
      '.hist-kop strong{min-width:44px;text-align:center;font-size:15px}.hist-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin-top:10px}' +
      '.mnd{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:2px;padding:9px 10px;text-align:left;min-height:64px}.mnd .naam{font-weight:600}.mnd small{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}' +
      '.mnd .vink{position:absolute;right:8px;bottom:8px;width:16px;height:16px;border:1.5px solid var(--line);border-radius:4px}.mnd.sel{border-color:var(--purple);background:var(--soft)}.mnd.sel .vink{background:var(--button);border-color:var(--button)}' +
      '.mnd.sel .vink::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}' +
      '.mnd .teken{position:absolute;right:9px;top:7px;font-weight:700}.st-ok .teken,.st-lopend .teken{color:var(--green)}.st-ok{border-color:var(--green)}.st-lopend{border:1px dashed var(--green)}.st-lopend .teken{opacity:.55}.st-deels .teken{color:#c98a12}.st-fout .teken{color:var(--red)}.st-leeg .teken,.st-proef .teken{color:var(--muted)}' +
      '.mnd.bezig{border-color:var(--purple);animation:bembpuls 1.2s ease-in-out infinite}@keyframes bembpuls{50%{background:var(--soft)}}' +
      '.balk{height:6px;border-radius:3px;background:var(--soft);overflow:hidden;margin-bottom:4px}.balk span{display:block;height:100%;width:0;background:var(--button);transition:width .3s}' +
      '#hist-tennet{margin-top:10px}#hist-tennet details.tn{margin-top:6px;font-size:12px}#hist-tennet table{font-size:11px}#hist-tennet td,#hist-tennet th{padding:3px 6px;white-space:nowrap}' +
      '#hist-stroom{max-height:320px}@media(max-width:767px){.hist-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}' +
      'details summary{cursor:pointer;font-weight:600}.uitleg p{font-size:13px;margin-top:8px}.mobile-menu{display:none}' +
      '@media(max-width:767px){.page{padding:12px 10px 28px}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.tile strong{font-size:18px}.panel{padding:12px}button,input[type=month],input[type=password]{min-height:44px;font-size:16px}h1{font-size:20px}}' +
      '</style><div class="page"><div class="wrap">' +
      '<header class="top"><h1>Mijnbatterij <small style="font-size:11px;font-weight:500">' + VERSIE + '</small><span id="pill-auto" class="pill"></span><span id="pill-proef" class="pill warn"></span></h1>' +
      '<div class="row">' + knop('Nu delen', 'id="nu" class="primary"') + knop('↻', 'id="ververs" aria-label="Vernieuwen"') + '</div></header>' +
      '<div id="status" class="notice" role="status" aria-live="polite"></div>' +
      '<section class="panel"><h2>Vandaag <small id="live-info" style="font-weight:400;font-size:12px"></small></h2><div class="tiles" id="tiles"></div></section>' +
      '<section class="panel"><h2>Afgelopen dagen</h2><div class="table-wrap"><table><thead><tr><th>Datum</th><th>Bron</th><th>Dekking</th><th>Handel</th><th>Besparing</th><th>Totaal</th><th>Geladen</th><th>Ontladen</th><th>Verstuurd</th><th>Op Mijnbatterij</th></tr></thead><tbody id="dagen"></tbody></table></div>' +
      '<div class="actions">' + knop('Vergelijk met Mijnbatterij', 'id="vergelijk"') + '<span class="notice" id="vergelijk-info" style="margin:0;align-self:center">Klik op een dag voor de exacte payload en wat Mijnbatterij opsloeg.</span></div><pre id="maand-mb" style="margin-top:8px"></pre>' +
      '<p class="notice">Tot BE een dag als complete markeert (ca. 48 uur) komt de dag uit de metingen in deze browser: de eigen cache plus, alleen-lezend, de coulombtelling en het netarchief van Balansenergie All-in. Een voorlopige dag gaat pas mee bij minstens 99% dekking. Daarna wordt hij met de BE-kwartierdata opnieuw berekend en overschreven.</p></section>' +
      '<section class="panel"><h2>Historie per maand</h2>' +
      '<div class="row hist-kop">' + knop('‹', 'id="hist-vorig" aria-label="Vorig jaar"') + '<strong id="hist-jaar"></strong>' + knop('›', 'id="hist-volgend" aria-label="Volgend jaar"') +
      '<span style="flex:1"></span>' + knop('Alles dit jaar', 'id="hist-alles"') + knop('Niets', 'id="hist-niets"') + '</div>' +
      '<div class="hist-grid" id="hist-grid" role="group" aria-label="Maanden"></div>' +
      '<div class="row" style="margin-top:12px">' + knop('Verstuur', 'id="hist-go" class="primary"') + knop('Stoppen', 'id="hist-stop" hidden') + '</div>' +
      '<div id="hist-voortgang" hidden style="margin-top:10px"><div class="balk"><span id="hist-balk"></span></div><small id="hist-voortgang-tekst"></small></div>' +
      '<pre id="hist-stroom" hidden aria-live="polite"></pre>' +
      '<div id="hist-tennet" hidden></div>' +
      '<p class="notice">Vink maanden aan en verstuur ze: per dag zie je hieronder wat er gebeurt. ✓ met groene rand = gelukt (maandtotaal staat op Mijnbatterij), ✓ met stippelrand = bijgewerkt, maandtotaal volgt als de maand compleet is, ! = niet alle dagen bruikbaar (maandtotaal niet vervangen), ✕ = mislukt of gestopt, – = geen BE-dagen. De status blijft bewaard. Ca. 4 s per dag, om binnen de verzoeklimiet van BE te blijven; dagen die nauwkeuriger op Mijnbatterij staan worden niet overschreven. Maandtotalen (ook handmatig ingevulde) worden alleen vervangen als alle dagen van de maand berekend konden worden.</p></section>' +
      '<section class="panel"><h2>Instellingen</h2>' +
      '<div class="row"><input id="key" type="password" autocomplete="off" placeholder="Mijnbatterij API-sleutel (profielpagina)">' + knop('Opslaan', 'id="key-save"') + knop('Controleren', 'id="key-test"') + knop('Wissen', 'id="key-wis"') + '</div>' +
      '<p class="notice" id="key-state"></p>' +
      schakel('c-auto', 'Automatisch delen in deze browser', 'Live elke 2 minuten, dagresultaten elk half uur. Zet dit maar in één browser aan; twee verzenders overschrijven elkaar.') +
      schakel('c-proef', 'Proefstand', 'Live en dagen gaan met test: true naar Mijnbatterij: gevalideerd, niet opgeslagen. Maandtotalen worden in proefstand niet verstuurd.') +
      schakel('c-saldeert', 'EB op teruglevering (t/m 2026)', 'Terugleverprijs inclusief energiebelasting zolang salderen geldt. Vanaf 2027 altijd zonder.') +
      schakel('c-afrr', 'aFRR-vergoedingen meetellen als handel', 'balance_energie_eur + deal_customer_share_eur uit de BE-dagbron; alleen bij definitieve dagen bekend.') +
      '<div class="row" style="margin-top:14px"><label class="muted" for="c-afstemming" style="font-size:12px">Afstemming met Home Assistant</label><select id="c-afstemming" style="border:1px solid var(--line);border-radius:7px;background:var(--paper);padding:8px 10px;min-height:38px"><option value="auto">Automatisch: HA verstuurt als die actief is</option><option value="altijd">Altijd zelf versturen</option></select></div>' +
      '<p class="notice" id="ha-state"></p></section>' +
      '<details class="panel uitleg"><summary>Rekenregels</summary>' +
      '<p>Per meetsegment krijgt het huis eerst de zonnestroom. Laadt de accu, dan eerst uit het zonne-overschot (gewaardeerd tegen de terugleverprijs die je misloopt) en de rest uit het net (afnameprijs). Ontladen dekt eerst het huis (vermeden afnameprijs), de rest gaat naar het net (terugleverprijs).</p>' +
      '<p>Besparing = ontladen naar huis × afnameprijs − het deel van de laadkosten dat bij die kWh hoort. Handel = ontladen naar net × terugleverprijs − het overige deel van de laadkosten, plus aFRR-vergoedingen. batteryResult = handel + besparing; Mijnbatterij leidt handel daaruit af.</p>' +
      '<p>All-in: kale BE-prijs × 1,21 ± € 0,02 opslag, plus energiebelasting op afname (2025 € 0,12286, 2026 € 0,11085, 2027 € 0,10650 incl. btw). Live en voorlopige dagen gebruiken de onbalans-/aFRR-/day-ahead-prijzen die deze browser heeft vastgelegd; definitieve dagen de effectieve kwartierprijzen van BE.</p>' +
      '<p>BE meet het accuvermogen aan de DC-kant. Voor de euro\'s telt de AC-kant: ontladen levert DC × η-uit aan huis/net, laden kost DC ÷ η-in van het net. η wordt per vermogensklasse gemeten op momenten zonder PV, waar de huisbalans exact is (accu AC = verbruik − net). Geladen/ontladen kWh richting Mijnbatterij blijven de gemeten DC-waarden, zoals BE ze rapporteert.</p>' +
      '<p>Definitieve BE-kwartieren geven bruto kWh maar niet de volgorde binnen het kwartier. Waar de eigen cache het kwartier dekt, wordt die verdeling gebruikt; anders de netto accubeweging per kwartier. Conversieverliezen zitten in de gemeten stromen.</p></details>' +
      '<details class="panel"><summary>Activiteit</summary><pre id="log"></pre></details>' +
      '</div></div>';
    document.body.appendChild(root);
    thema();
    var c = cfg();
    [['c-auto', 'auto'], ['c-proef', 'proef'], ['c-saldeert', 'saldeert'], ['c-afrr', 'afrr']].forEach(function (p) {
      $(p[0]).checked = !!c[p[1]];
      $(p[0]).onchange = function () {
        zetCfg(p[1], $(p[0]).checked);
        // Rekenwijzigingen: niets ongemerkt doorsturen.
        if (p[1] === 'saldeert' || p[1] === 'afrr') { zetCfg('proef', true); $('c-proef').checked = true; status('Rekenwijziging: proefstand weer aan.'); }
        render(); if (p[1] === 'auto' && $(p[0]).checked) tik();
      };
    });
    $('vergelijk').onclick = async function () {
      var b = $('vergelijk'); b.disabled = true; $('vergelijk-info').textContent = 'Ophalen bij Mijnbatterij…';
      try { var n = await vergelijkMijnbatterij(); $('vergelijk-info').textContent = n + ' dagen opgehaald om ' + klok(mbOpgehaald) + '. ✓ = totaal en kWh gelijk aan wat wij stuurden, ⚠ = afwijkend. Besparing geeft Mijnbatterij alleen per maand terug.'; render(); }
      catch (e) { $('vergelijk-info').textContent = e.message; } finally { b.disabled = false; }
    };
    $('dagen').onclick = function (e) {
      var tr = e.target.closest('tr.dag'); if (!tr) return;
      var open = tr.nextElementSibling && tr.nextElementSibling.classList.contains('detail');
      shadow.querySelectorAll('tr.detail').forEach(function (x) { x.remove(); });
      if (open) return;
      var det = document.createElement('tr'); det.className = 'detail';
      det.innerHTML = '<td colspan="10"><strong>Grootste kwartierbijdragen (all-in prijzen)</strong><pre>' + esc(tr.dataset.top || '—') + '</pre><strong>Verstuurd</strong><pre>' + esc(tr.dataset.payload || 'Nog niets verstuurd voor deze dag.') + '</pre><strong>Op Mijnbatterij</strong><pre>' + esc(tr.dataset.mb || 'Niet opgehaald of niet aanwezig. Gebruik “Vergelijk met Mijnbatterij”.') + '</pre></td>';
      tr.insertAdjacentElement('afterend', det);
    };
    $('c-afstemming').value = c.afstemming || 'auto';
    $('c-afstemming').onchange = function () { zetCfg('afstemming', $('c-afstemming').value); render(); };
    $('key-save').onclick = async function () {
      try { var a = await brug({ op: 'key-set', value: $('key').value }); if (!a.ok) throw Error(a.error); $('key').value = ''; heeftSleutel = !!a.hasKey; status(heeftSleutel ? 'Sleutel opgeslagen in de userscript-opslag.' : 'Sleutel gewist.'); render(); }
      catch (e) { status(e.message); }
    };
    $('key-wis').onclick = async function () { try { await brug({ op: 'key-set', value: '' }); heeftSleutel = false; status('Sleutel gewist.'); render(); } catch (e) { status(e.message); } };
    $('key-test').onclick = async function () {
      status('Sleutel controleren…');
      try { var a = await mb('GET', '/api/me'), j = {}; try { j = JSON.parse(a.text); } catch (e) {} status('Sleutel werkt' + (j.username || j.name ? ' · ' + (j.username || j.name) : '') + '.'); }
      catch (e) { status(e.message); }
    };
    $('nu').onclick = async function () {
      var b = $('nu'); b.disabled = true; status('Verzamelen en delen…');
      try { await verzamel(); var uit = []; try { await liveRonde(true); uit.push('Live ok.'); } catch (e) { uit.push('Live: ' + e.message); } uit.push(await dagRonde(true) || 'Dagen: er loopt al een ronde.'); status(uit.join(' · ').slice(0, 400)); }
      catch (e) { status(e.message); } finally { b.disabled = false; render(); }
    };
    $('ververs').onclick = async function () { status('Verzamelen…'); await verzamel(); render(); status('Bijgewerkt ' + klok(Date.now()) + '.'); };
    $('hist-grid').onclick = function (ev) {
      var b = ev.target.closest && ev.target.closest('button[data-ym]'); if (!b || b.disabled || historieBezig) return;
      var ym = b.getAttribute('data-ym'); if (histSelectie.has(ym)) histSelectie.delete(ym); else histSelectie.add(ym); tekenMaanden();
    };
    $('hist-vorig').onclick = function () { histJaar = (histJaar || +vandaag().slice(0, 4)) - 1; tekenMaanden(); };
    $('hist-volgend').onclick = function () { histJaar = (histJaar || +vandaag().slice(0, 4)) + 1; tekenMaanden(); };
    $('hist-alles').onclick = function () {
      var nu = vandaag().slice(0, 7), jaar = histJaar || +nu.slice(0, 4);
      for (var m = 1; m <= 12; m++) { var ym = jaar + '-' + String(m).padStart(2, '0'); if (ym <= nu && !(histStatus[ym] && histStatus[ym].st === 'leeg')) histSelectie.add(ym); }
      tekenMaanden();
    };
    $('hist-niets').onclick = function () { histSelectie.clear(); tekenMaanden(); };
    $('hist-go').onclick = async function () {
      try { status('Historie: ' + await verstuurMaanden(Array.from(histSelectie))); }
      catch (e) { status(e.message); tekenMaanden(); }
    };
    $('hist-stop').onclick = function () { historieStop = true; status('Stoppen na de huidige dag…'); };
    laadHistStatus().then(tekenMaanden);
    $('log').textContent = log.join('\n') || 'Nog geen activiteit.';
  }
  var renderBezig = false;
  async function render() {
    if (!opened || !shadow || renderBezig) return;
    renderBezig = true;
    try {
      var c = cfg(), id = klant();
      $('pill-auto').textContent = haNeemtOver(c) ? 'Home Assistant verstuurt' : c.auto ? 'automatisch' : 'handmatig';
      $('ha-state').textContent = haStatus.gecontroleerd ? (haStatus.at ? 'Laatste Home Assistant-heartbeat: ' + new Date(haStatus.at).toLocaleString('nl-NL', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) + (haStatus.actief ? ' — HA is actief, de browser verstuurt niets.' : ' — te oud, de browser verstuurt zelf.') : 'Geen actieve Home Assistant-heartbeat gezien; de browser verstuurt zelf. Draait de integratie wel, dan laat die het versturen aan de browser zolang deze nauwkeurigere dagen heeft (zie de status in HA).') : 'Nog niet gecontroleerd.';
      $('pill-proef').textContent = c.proef ? 'proefstand' : ''; $('pill-proef').hidden = !c.proef;
      $('key-state').textContent = heeftSleutel ? 'Sleutel ingesteld (staat in de userscript-opslag, niet in de pagina).' : 'Nog geen sleutel. Je vindt hem op je Mijnbatterij-profielpagina.';
      $('nu').textContent = c.proef ? 'Nu valideren (proef)' : 'Nu delen';
      if (!id) { $('tiles').innerHTML = '<p class="muted">Nog geen installatie herkend. Open eerst een dashboardpagina.</p>'; return; }
      var r = await dagUitCache(id, vandaag(), Date.now(), c), w = r.w;
      $('tiles').innerHTML =
        '<div class="tile"><small>Totaal</small><strong class="' + cls(w.totaal) + '">' + euro(w.totaal) + '</strong></div>' +
        '<div class="tile"><small>Handel</small><strong class="' + cls(w.handel) + '">' + euro(w.handel) + '</strong></div>' +
        '<div class="tile"><small>Besparing</small><strong class="' + cls(w.besparing) + '">' + euro(w.besparing) + '</strong></div>' +
        '<div class="tile"><small>Accudekking</small><strong>' + pct(r.dekking) + '</strong><div class="sub">' + kwhT(w.geladen) + ' in · ' + kwhT(w.ontladen) + ' uit</div></div>';
      $('live-info').textContent = (laatsteLive ? 'live ' + (laatsteLive.p.test ? 'gevalideerd ' : 'verstuurd ') + klok(laatsteLive.at) : 'nog niet live verstuurd') +
        (laatsteVerzameling ? ' · metingen ' + klok(laatsteVerzameling) : '') + (w.zonderPrijs ? ' · ' + w.zonderPrijs + ' kwartieren zonder prijs' : '') + ' · ' + etaTekst(ETA).toLowerCase() + (function () { var n = ETA.n ? ETA.n.uit.concat(ETA.n.in).reduce(function (a, b) { return a + b; }, 0) : 0; return n >= 100 ? ' (gemeten, n=' + n + ')' : ' (standaard' + (n ? ', ' + n + ' metingen, nog te weinig' : '') + ')'; })();
      var rijen = [];
      for (var i = 1; i <= DAG_VENSTER; i++) {
        var dk = plusDagen(vandaag(), -i), d = null, def = await lees(k(id, 'def:' + dk));
        try {
          if (def && def.v === 3 && def.kw) {
            var dd = verdeelDefinitief(def.kw, await cacheKwartieren(id, dk), ETA);
            var defRij = null; try { defRij = JSON.parse(def.sig); } catch (e) {}
            d = { bron: 'BE', dekking: 1, w: waardeer(kiesPrijzen(dd.kw, def.kw, await prijzenDag(id, dk, true), defRij), c, c.afrr ? afrrVan(defRij) : null) };
          } else d = await dagUitCache(id, dk, null, c, true);
        } catch (e) { d = null; }
        var st = await lees(k(id, (c.proef ? 'proef:' : 'sent:') + dk));
        var leeg = !d || (!d.w.accuSec && d.bron !== 'BE');
        var opg = mbOpgeslagen[dk], mbCel = '<span class="muted">—</span>', toon = null;
        if (st && st.payload) { toon = JSON.parse(JSON.stringify(st.payload)); if (Array.isArray(toon.intervals) && toon.intervals.length > 3) toon.intervals = toon.intervals.slice(0, 3).concat(['… ' + (st.payload.intervals.length - 3) + ' kwartieren meer']); }
        if (opg) {
          // De dag-GET kent geen batterySavings; controle op totaal en kWh.
          var mt = num(opg.batteryResult), ml = num(opg.batteryCharged), mo = num(opg.batteryDischarged), gp = st && st.payload, gelijk = gp ?
            Math.abs((mt || 0) - Number(gp.batteryResult)) < 0.011 && (ml === null || Math.abs(ml - Number(gp.chargedToday)) < 0.05) && (mo === null || Math.abs(mo - Number(gp.dischargedToday)) < 0.05) : null;
          mbCel = '<span class="' + (gelijk === true ? 'ok' : gelijk === false ? 'afw' : '') + '">' + (gelijk === true ? '✓ ' : gelijk === false ? '⚠ ' : '') + euro(mt) + '</span>' +
            ' <small class="muted">' + (ml !== null ? ml.toLocaleString('nl-NL', { maximumFractionDigits: 1 }) + '↑ ' : '') + (mo !== null ? mo.toLocaleString('nl-NL', { maximumFractionDigits: 1 }) + '↓' : '') + (opg.type ? ' · ' + esc(opg.type) : '') + ((leesLabel(opg.note) || {}).bron ? ' · ' + esc(leesLabel(opg.note).bron) : '') + '</small>';
        } else if (mbOpgehaald) mbCel = '<span class="muted">niet aanwezig</span>';
        var topTekst = d && d.w && d.w.top ? d.w.top.map(function (x) { return klok(x.q) + '  € ' + x.eur.toFixed(2) + '  · huis ' + x.ontHuis.toFixed(2) + ' / net ' + x.ontNet.toFixed(2) + ' uit, net ' + x.ladNet.toFixed(2) + ' / zon ' + x.ladPV.toFixed(2) + ' in kWh · afname € ' + (getal(x.pIn) ? x.pIn.toFixed(3) : '—') + ', teruglevering € ' + (getal(x.pUit) ? x.pUit.toFixed(3) : '—') + (x.bron ? ' (' + x.bron + ')' : ''); }).join('\n') : '';
        rijen.push('<tr class="dag" data-top="' + esc(topTekst) + '" data-payload="' + esc(toon ? JSON.stringify(toon, null, 2) : '') + '" data-mb="' + esc(opg ? JSON.stringify(opg, null, 2) : '') + '"><td>' + esc(fmtDatum.format(new Date(dk + 'T12:00:00Z'))) + '</td><td>' + (leeg ? '<span class="muted">geen data</span>' : '<span class="tag ' + (d.bron === 'BE' ? 'be">BE definitief' : 'cache">cache') + '</span>') + '</td>' +
          '<td>' + (leeg ? '—' : pct(d.dekking)) + '</td><td class="' + (leeg ? '' : cls(d.w.handel)) + '">' + (leeg ? '—' : euro(d.w.handel)) + '</td>' +
          '<td class="' + (leeg ? '' : cls(d.w.besparing)) + '">' + (leeg ? '—' : euro(d.w.besparing)) + '</td><td class="' + (leeg ? '' : cls(d.w.totaal)) + '"><strong>' + (leeg ? '—' : euro(d.w.totaal)) + '</strong></td>' +
          '<td>' + (leeg ? '—' : kwhT(d.w.geladen)) + '</td><td>' + (leeg ? '—' : kwhT(d.w.ontladen)) + '</td>' +
          '<td>' + (st ? esc((c.proef ? 'getest ' : '') + st.bron + ' · ' + new Date(st.at).toLocaleString('nl-NL', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })) : '<span class="muted">—</span>') + '</td><td>' + mbCel + '</td></tr>');
      }
      $('dagen').innerHTML = rijen.join('');
      var mr = Object.keys(mbMaanden).sort().map(function (ym) { var m = mbMaanden[ym];
        return ym + ' op Mijnbatterij: totaal ' + euro(num(m.batteryResult)) + ' · besparing ' + euro(num(m.batterySavings)) + ' · handel (afgeleid) ' + euro((num(m.batteryResult) || 0) - (num(m.batterySavings) || 0) - (num(m.batteryResultCongestion) || 0)) +
          (num(m.batteryResultFixed) !== null ? ' · fixed ' + euro(num(m.batteryResultFixed)) : '') + (m.partial ? ' · deelresultaat' : ''); });
      $('maand-mb').textContent = mr.join('\n');
      $('log').textContent = log.join('\n') || 'Nog geen activiteit.';
    } catch (e) { status(e.message); }
    finally { renderBezig = false; }
  }
  function mobielToggle() { return document.querySelector('[data-bs-target="#mobileSidebar"]'); }
  function layout() {
    if (!root || !opened) return;
    if (innerWidth >= 768) {
      var anchor = navLinks.find(function (a) { var r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.left < innerWidth; });
      var c = anchor && anchor.closest('nav,aside,.sidebar,.sidenav,#sidebar,.offcanvas,.nav'), left = 0;
      while (c && c !== document.body) { var r = c.getBoundingClientRect(); if (r.width > 80 && r.width < 440 && r.height > 180 && r.left < 120 && r.right < innerWidth * 0.6) left = Math.max(left, r.right); c = c.parentElement; }
      root.style.left = Math.round(left) + 'px'; root.style.top = '0px';
    } else {
      var t = mobielToggle(), h = t && t.closest('header,nav,.navbar');
      root.style.left = '0px'; root.style.top = Math.max(0, Math.round(h ? h.getBoundingClientRect().bottom : 0)) + 'px';
    }
  }
  function markeer(aan) {
    if (aan) {
      activeBefore = Array.from(document.querySelectorAll('a.active')).filter(function (a) { return navLinks.indexOf(a) < 0 && a.closest('nav,aside,.sidebar,.sidenav,#sidebar,#mobileSidebar,.nav'); })
        .map(function (a) { var o = a.className; a.className = o.replace(/\bactive\b/g, '').trim(); return [a, o]; });
      navLinks.forEach(function (a) { a.classList.add('active'); a.setAttribute('aria-current', 'page'); });
    } else {
      navLinks.forEach(function (a) { a.classList.remove('active'); a.removeAttribute('aria-current'); });
      activeBefore.forEach(function (p) { if (p[0].isConnected) p[0].className = p[1]; }); activeBefore = [];
    }
  }
  function open(push) {
    mount();
    var t = document.querySelector('[data-bs-target="#mobileSidebar"][aria-expanded="true"]');
    if (innerWidth < 768 && t) t.click();
    if (push && location.hash !== HASH) history.pushState({ bemb: true }, '', location.pathname + location.search + HASH);
    if (opened) { layout(); return; }
    opened = true; root.hidden = false; thema(); layout(); markeer(true);
    if (innerWidth < 768) window.scrollTo(0, 0);
    render(); sleutelStatus().then(render);
  }
  function close() {
    if (!opened) return;
    opened = false; root.hidden = true; markeer(false);
    if (location.hash === HASH) history.replaceState(history.state, '', location.pathname + location.search);
  }
  var ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex:none"><rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 10.5v3"/><path d="M11.5 9 9 12.5h3.5L10 15"/></svg>';
  function addMenu() {
    navLinks = navLinks.filter(function (a) { return a.isConnected; });
    var ankers = Array.from(document.querySelectorAll('a[href*="/results"]')).filter(function (a) { return !a.hasAttribute('data-bemb-link') && !a.closest('#bemb-root,#be-energieoverzicht'); });
    ankers.forEach(function (anker) {
      var lijst = anker.closest('ul,ol,nav,aside,.sidebar,.sidenav,#sidebar,#mobileSidebar,.nav') || anker.parentElement;
      if (navLinks.some(function (a) { return lijst.contains(a); })) return;
      var basis = anker.className.split(/\s+/).filter(function (x) { return x && x !== 'active'; })[0];
      var link = document.createElement('a');
      link.id = 'bemb-link' + (menuSerial ? '-' + menuSerial : ''); menuSerial++;
      link.href = HASH; link.setAttribute('data-bemb-link', '');
      link.className = anker.className.replace(/\bactive\b/g, '').trim();
      link.innerHTML = ICON + '<span>Mijnbatterij</span>';
      var icoon = anker.querySelector('svg'), eigen = link.querySelector('svg');
      if (icoon) { eigen.setAttribute('class', icoon.getAttribute('class') || ''); eigen.setAttribute('width', icoon.getAttribute('width') || '24'); eigen.setAttribute('height', icoon.getAttribute('height') || '24'); }
      link.style.cssText = 'display:flex;align-items:center;gap:12px;white-space:normal';
      link.title = 'Accuresultaat delen met mijnbatterij.nl (handel en besparing)';
      link.addEventListener('click', function (e) { e.preventDefault(); open(true); });
      // Onderaan het menu: na het laatste item met dezelfde linkklasse.
      var li = anker.parentElement && anker.parentElement.tagName === 'LI' ? anker.parentElement : null;
      if (li && li.parentElement) {
        var items = Array.from(li.parentElement.children).filter(function (x) { var a = x.querySelector('a'); return a && (!basis || a.classList.contains(basis)); });
        var laatste = items[items.length - 1] || li, nieuw = document.createElement('li');
        nieuw.className = li.className.replace(/\bactive\b/g, '').trim(); nieuw.appendChild(link);
        laatste.insertAdjacentElement('afterend', nieuw);
      } else {
        var broers = Array.from(anker.parentElement.children).filter(function (x) { return x.tagName === 'A' && (!basis || x.classList.contains(basis)); });
        (broers[broers.length - 1] || anker).insertAdjacentElement('afterend', link);
      }
      navLinks.push(link);
      if (opened) { link.classList.add('active'); link.setAttribute('aria-current', 'page'); }
    });
  }
  function onderhoud() {
    addMenu();
    if (location.hash === HASH && !opened) open(false);
    else if (location.hash !== HASH && opened) close();
    if (opened) layout();
  }
  // Andere menulinks sluiten deze pagina; hun eigen navigatie gaat gewoon door.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (opened && a && navLinks.indexOf(a) < 0 && !(root && root.contains(a)) && a.getAttribute('href') !== HASH) close();
  }, true);
  window.addEventListener('hashchange', onderhoud); window.addEventListener('popstate', onderhoud);
  window.addEventListener('resize', function () { clearTimeout(layout.t); layout.t = setTimeout(layout, 60); });
  document.addEventListener('be-dark-change', thema);
  new MutationObserver(thema).observe(document.documentElement, { attributes: true, attributeFilter: ['data-be-dark'] });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tik(); });
  setInterval(onderhoud, 1500); onderhoud();
  laadRendement(klant());
  sleutelStatus().then(function () { setTimeout(tik, 4000); });
  setInterval(tik, 120000);
}

(function () {
  'use strict';
  function launch() {
    var s = document.createElement('script');
    s.textContent = '(' + bembPage.toString() + ')();';
    document.documentElement.appendChild(s); s.remove();
  }
  if (document.body) launch(); else document.addEventListener('DOMContentLoaded', launch, { once: true });
})();
