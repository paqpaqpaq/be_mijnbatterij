// Lokale test van de maandkalender: nep-BE (XHR) + nep-Mijnbatterij (GM), echte plugincode in jsdom.
require('fake-indexeddb/auto');
const {JSDOM}=require('jsdom');const fs=require('fs');
const src=fs.readFileSync('/home/claude/be_mijnbatterij/userscript/BE-Mijnbatterij.user.js','utf8').replace(/^\/\/ ==UserScript==[\s\S]*?==\/UserScript==/,'');
const html=`<html><body><aside class="sidebar"><ul class="nav"><li class="nav-item"><a class="nav-link" href="/customer/ABC123/results">Resultaten</a></li></ul></aside><main>x</main></body></html>`;
const VANDAAG=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Amsterdam'}).format(new Date());
const off=t=>{const s=new Intl.DateTimeFormat('en-US',{timeZone:'Europe/Amsterdam',timeZoneName:'longOffset'}).formatToParts(new Date(t)).find(p=>p.type==='timeZoneName').value;return s==='GMT'?'+00:00':s.slice(3);};
const start=dk=>{let t=Date.parse(dk+'T00:00:00Z');return Date.parse(dk+'T00:00:00'+off(t-3600e3));};
const plus=(dk,n)=>{const d=new Date(dk+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);};
const iso=t=>{const o=off(t);const d=new Date(t+ (o[0]==='-'?-1:1)*(+o.slice(1,3)*3600e3+ +o.slice(4,6)*60e3));return d.toISOString().slice(0,19)+o;};
const EERSTE='2026-09-10', KAPOT='2026-10-05';
function maand(ym){const dagen=[];for(let d=ym+'-01';d.slice(0,7)===ym&&d<=VANDAAG;d=plus(d,1))if(d>=EERSTE)dagen.push(d);
  return {interval:'month',period:ym,timezone:'Europe/Amsterdam',pricing:{include_vat:false,include_delivery_cost:false},
    series:dagen.map(d=>({date:d,status:d<plus(VANDAAG,-1)?'complete':'provisional',battery_charged_kwh:30,battery_discharged_kwh:20,avg_buy_price_eur_per_kwh:0.11,avg_sell_price_eur_per_kwh:0.09,balance_energie_eur:0.4}))};}
function dag(dk){const s=start(dk),e=start(plus(dk,1)),iv=[];
  for(let q=s;q<e;q+=9e5){const h=Math.floor((q-s)/3600e3);const lad=(h<5&&(q-s)%3600e3<2700e3)?2:0, ont=(h>=17&&h<21)?1.25:0;
    const kapot=dk===KAPOT&&h>=17&&h<19;
    iv.push({timestamp:iso(q),grid_import_kwh:lad+0.2,grid_export_kwh:ont*0.6,battery_charged_kwh:kapot?null:lad,battery_discharged_kwh:kapot?null:ont,effective_import_price_eur_per_kwh:0.12,effective_export_price_eur_per_kwh:0.3});}
  return {interval:'day',period:dk,pricing:{include_vat:false,include_delivery_cost:false},day_detail:{intervals:iv}};}
const mbDag={},mbMaand={},log={be:0,mb:[]};
// bestaand nauwkeuriger record van HA op 15 sep: mag niet overschreven worden
mbDag['2026-09-15']={date:'2026-09-15',batteryResult:'9.99',note:'x [BEMB v=1.3.0 bron=ha type=be dekking=100.0 kwal=99.5]'};
function maakDom(){
  const dom=new JSDOM(html,{url:'https://dashboard.balansenergie.nl/customer/ABC123/results',runScripts:'dangerously',pretendToBeVisual:true});
  const w=dom.window; w.indexedDB=indexedDB; w.IDBKeyRange=IDBKeyRange;
  const st=w.setTimeout.bind(w); w.setTimeout=(f,ms,...a)=>st(f,(ms>=1000&&ms<=5000)?0:ms,...a); // pauzes (1,25/4 s) inkorten
  class XHR{open(m,u){this.u=new URL(u);}setRequestHeader(){}getResponseHeader(){return null}send(){st(()=>{log.be++;const p=this.u.searchParams;let j={};
      if(this.u.pathname.includes('results-v2-api'))j=p.get('interval')==='month'?maand(p.get('period')):dag(p.get('period'));
      else if(this.u.pathname.includes('current-history'))j={rows:[]};else if(this.u.pathname.includes('current-prices'))j={prices:[]};
      this.status=200;this.responseText=JSON.stringify(j);this.onload&&this.onload();},1);}}
  w.XMLHttpRequest=XHR;
  w.__s={bemb_key:'mb_test',bemb_cfg:undefined};
  w.__gm=(o)=>{const u=new URL(o.url);let body={};const d=o.data?JSON.parse(o.data):null;log.mb.push(o.method+' '+u.pathname);
    if(o.method==='GET'&&u.pathname==='/api/results/daily'){const f=u.searchParams.get('from'),t=u.searchParams.get('to');body={results:Object.values(mbDag).filter(r=>r.date>=f&&r.date<=t)};}
    else if(o.method==='POST'&&u.pathname==='/api/results/daily'){mbDag[d.date]=d;body={ok:true};}
    else if(o.method==='POST'&&u.pathname==='/api/results/monthly'){mbMaand[d.yearMonth]=d;body={ok:true};}
    st(()=>o.onload({status:200,responseText:JSON.stringify(body)}),1);};
  w.eval(`var GM_getValue=(k,d)=>window.__s[k]??d,GM_setValue=(k,v)=>{window.__s[k]=v},GM_deleteValue=k=>{delete window.__s[k]},GM_xmlhttpRequest=o=>window.__gm(o);`+src);
  return w;
}
const slaap=ms=>new Promise(r=>setTimeout(r,ms));
let fouten=0;const chk=(c,m)=>{console.log((c?'ok   ':'FOUT ')+m);if(!c)fouten++;};
(async()=>{
  let w=maakDom(); await slaap(1500);
  w.document.querySelector('[data-bemb-link]').click(); await slaap(600);
  let sh=w.document.getElementById('bemb-root').shadowRoot, $=id=>sh.getElementById(id);
  // proefstand uit (standaard aan in de plugin)
  const proef=$('c-proef'); if(proef&&proef.checked){proef.click();await slaap(100);}
  const cel=ym=>sh.querySelector(`button[data-ym="${ym}"]`);
  chk($('hist-jaar').textContent==='2026','jaar 2026 getoond');
  chk(cel('2026-12').disabled,'december (toekomst) uitgeschakeld');
  for(const ym of ['2026-08','2026-09','2026-10','2026-11'])cel(ym).click();
  await slaap(50);
  chk($('hist-go').textContent==='Verstuur 4 maanden','knoptekst telt selectie');
  $('hist-go').click(); await slaap(30);
  chk(!$('hist-stop').hidden,'stopknop zichtbaar tijdens versturen');
  for(let i=0;i<400&&!$('hist-stop').hidden;i++)await slaap(50);
  const stroom=$('hist-stroom').textContent;
  const tekens=ym=>cel(ym).querySelector('.teken').textContent;
  chk(tekens('2026-08')==='–','aug: geen BE-dagen (–)');
  chk(tekens('2026-09')==='✓'&&cel('2026-09').classList.contains('st-ok'),'sep: gelukt (✓, groen)');
  chk(tekens('2026-10')==='!','okt: deels (! — 5 okt niet bruikbaar)');
  chk(tekens('2026-11')==='✓'&&cel('2026-11').classList.contains('st-lopend'),'nov: lopende maand bijgewerkt (✓ stippel)');
  chk(!!mbMaand['2026-09']&&!mbMaand['2026-10']&&!mbMaand['2026-11'],'alleen maandtotaal sep verstuurd');
  chk(mbDag['2026-09-15'].batteryResult==='9.99','nauwkeuriger HA-record 15 sep niet overschreven');
  chk(/2026-09-15 .*niet vervangen/.test(stroom),'stroom meldt "niet vervangen" voor 15 sep');
  chk(/2026-10-05 .*overgeslagen/.test(stroom),'stroom meldt 5 okt overgeslagen');
  chk(/2026-09 .*maandtotaal .* verstuurd ✓/.test(stroom),'stroom meldt maandtotaal sep verstuurd');
  chk(sh.querySelectorAll('button.mnd.sel').length===0,'selectie leeg na afloop');
  const nDagen=Object.keys(mbDag).filter(d=>d.startsWith('2026-09')).length;
  chk(nDagen===21,'21 septemberdagen op Mijnbatterij ('+nDagen+')');
  {const sec=[...sh.querySelectorAll('section.panel')].find(x=>/Historie per maand/.test(x.textContent));const css=sh.querySelector('style').textContent;
   fs.writeFileSync('hist_panel.html','<!doctype html><meta charset=utf-8><body style="margin:0"><div id=h></div><script>const r=document.getElementById("h").attachShadow({mode:"open"});r.innerHTML='+JSON.stringify('<style>'+css+'</style><div class="page"><div class="wrap">'+sec.outerHTML+'</div></div>')+';</script>');}
  console.log('--- stroom (begin/eind) ---\n'+stroom.split('\n').slice(0,4).join('\n')+'\n…\n'+stroom.split('\n').slice(-6).join('\n'));
  // tweede keer sep: alles ongewijzigd, maand ongewijzigd
  const postsVoor=log.mb.filter(x=>x.startsWith('POST')).length;
  cel('2026-09').click(); $('hist-go').click(); await slaap(30); for(let i=0;i<200&&!$('hist-stop').hidden;i++)await slaap(50);
  chk(log.mb.filter(x=>x.startsWith('POST')).length===postsVoor,'opnieuw sep: niets opnieuw gepost (ongewijzigd)');
  chk(/maandtotaal .*ongewijzigd ✓/.test($('hist-stroom').textContent),'opnieuw sep: maandtotaal ongewijzigd ✓');
  // stoppen midden in een maand
  cel('2026-10').click(); $('hist-go').click(); await slaap(40); $('hist-stop').click();
  for(let i=0;i<200&&!$('hist-stop').hidden;i++)await slaap(50);
  chk(tekens('2026-10')==='✕','okt na stoppen: ✕ (gestopt)');
  chk(cel('2026-10').classList.contains('sel'),'gestopte maand blijft aangevinkt');
  // onthouden: nieuwe pagina, zelfde opslag
  w.close(); w=maakDom(); await slaap(1500); w.document.querySelector('[data-bemb-link]').click(); await slaap(800);
  sh=w.document.getElementById('bemb-root').shadowRoot;
  const t2=ym=>sh.querySelector(`button[data-ym="${ym}"]`).querySelector('.teken').textContent;
  chk(t2('2026-09')==='✓'&&t2('2026-08')==='–'&&t2('2026-11')==='✓'&&t2('2026-10')==='✕','status onthouden na herladen');
  chk(/gelukt/.test(sh.querySelector('button[data-ym="2026-09"]').title),'tooltip sep: '+sh.querySelector('button[data-ym="2026-09"]').title);
  console.log('BE-verzoeken:',log.be,'| FOUTEN:',fouten); process.exit(fouten?1:0);
})().catch(e=>{console.error(e);process.exit(1)});
