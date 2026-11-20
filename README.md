<img src="custom_components/be_mijnbatterij/brand/icon.png" width="96" align="right" alt="">

# BE Mijnbatterij (Home Assistant)

Stuurt het accuresultaat van een **Balans Energie**-installatie naar **mijnbatterij.nl**, gesplitst in *handel* en *besparing*. De integratie logt zelf in op het BE-klantdashboard. Daardoor loopt het delen door, ook als niemand het dashboard open heeft.

Rekent exact zoals de browserplugin **BE-Mijnbatterij.user.js** (≥ 1.1.0). De twee werken samen: als Home Assistant draait, verstuurt Home Assistant. Valt Home Assistant weg, dan neemt de plugin het vanzelf over.

## Browserplugin

De plugin staat in [`userscript/BE-Mijnbatterij.user.js`](userscript/BE-Mijnbatterij.user.js). Installeer hem met Tampermonkey of Userscripts; daarna werkt hij zichzelf bij via deze repository. Hij draait naast Balansenergie All-in en verandert daar niets aan.

## Installatie (HACS)

1. HACS → *Custom repositories* → deze repository toevoegen, type *Integration*.
2. **BE Mijnbatterij** installeren en Home Assistant herstarten.
3. *Instellingen → Apparaten & diensten → Integratie toevoegen → BE Mijnbatterij*.
4. Vul in:
   - je gebruikersnaam en wachtwoord van het BE-dashboard;
   - je Mijnbatterij API-sleutel (te vinden op je profielpagina).

Bij het instellen kies je of je eerst wilt proberen (**proefstand**, standaard uit). In proefstand gaat alles met `test: true` naar Mijnbatterij: het wordt gecontroleerd, maar niet opgeslagen. Alle instellingen staan als schakelaars op het apparaat *BE Mijnbatterij*: versturen, proefstand, EB op teruglevering en aFRR als handel. Zolang de proefstand aan staat, staat er een melding onder *Instellingen → Reparaties*, zodat hij niet ongemerkt aan blijft.

## Wat de integratie doet

| Interval | Actie |
|---|---|
| elke minuut | nieuwe metingen ophalen (`current-history`); kwartieren worden incrementeel opgebouwd |
| elke 2 min | prijzen archiveren (`current-prices`), live-resultaat naar `/api/live` |
| elke 30 min | de laatste 10 dagen: definitief uit BE-kwartierdata zodra BE de dag als `complete` markeert (ca. 48 uur), anders voorlopig uit de eigen metingen |
| elke 15 min | heartbeat: zie *Samenwerking met de plugin* |

**Entiteiten**
- **Sensoren:** resultaat, handel en besparing vandaag; geladen en ontladen vandaag; SOC; meetdekking; laatste live-verzending; status. De status bevat de activiteitenlog en de verstuurde dagen als attributen.
- **Knop:** *Nu versturen*.
- **Service:** `be_mijnbatterij.historie_herstellen` met `vanaf: "2026-08"`. Deze stuurt alle definitieve BE-dagen vanaf die maand opnieuw en vervangt de maandtotalen van volledig berekende maanden (ook handmatig ingevulde). De actie start de historie in de achtergrond en keert direct terug; de voortgang staat in de activiteit van de status-sensor en aan het eind volgt een melding. Tussen BE-verzoeken zit dan 4 seconden (de plugin doet hetzelfde), en een dag die op Mijnbatterij nauwkeuriger staat, wordt niet overschreven.
- **Hoe compleet is de historie:** geladen/ontladen kWh komen exact uit BE, de settlementprijzen per kwartier uit BE en TenneT (zie hieronder). Voor dagen zonder eigen metingen (ouder dan ongeveer een maand) wordt de verdeling over huis en net geschat in kwartieren waarin de accu zowel laadde als ontlaadde. De besparing valt daardoor eerder te laag dan te hoog uit; zulke dagen krijgen een lage `kwal` en bij meer dan 8 geschatte kwartieren `invalid`.

## Rekenregels

- **Verdeling per meetsegment:** het huis krijgt eerst de zonnestroom. De accu laadt eerst uit het zonne-overschot, gewaardeerd tegen de gemiste terugleverprijs, en daarna uit het net tegen de afnameprijs. Ontladen dekt eerst het huis (vermeden afname); de rest gaat naar het net (teruglevering).
- **Besparing** = ontladen naar huis × afnameprijs − het deel van de laadkosten dat daarbij hoort.
- **Handel** = ontladen naar net × terugleverprijs − de overige laadkosten + aFRR-vergoedingen.
- **batteryResult** = handel + besparing. Mijnbatterij leidt de handel daaruit af.
- **Omvormerrendement:** BE meet het accuvermogen aan de DC-kant. Gewaardeerd wordt de AC-kant: ontladen levert DC × η-uit, laden kost DC ÷ η-in. Sinds 1.4.1 is η vast per installatie, afgeleid uit BE's eigen energiebalans (verbruik + teruglevering − import − zon tegen BE's DC-accucijfers, 73 volledige dagen): η-uit 0,925, η-in 0,969, rondgang 0,896. Daardoor rekenen elke browser en Home Assistant met hetzelfde rendement en geeft dezelfde BE-dag overal hetzelfde resultaat. De eigen meting per browser/HA staat er alleen ter informatie bij. De geladen en ontladen kWh richting Mijnbatterij blijven de DC-waarden, zoals BE ze rapporteert.
- **All-in prijs:** kale BE-prijs × 1,21 ± € 0,02 opslag, plus energiebelasting op afname. Tot en met 2026 komt de EB ook terug op teruglevering (saldering).
- **Prijs per kwartier:** de settlementprijs uit BE's market_breakdown; anders de effectieve kwartierprijs van BE als die betrouwbaar is (minstens 0,05 kWh netstroom en hooguit € 1,50/kWh kaal — die grens geldt alleen voor deze kosten/kWh-deling, niet voor settlementprijzen, die geregeld € 4–5/kWh halen). Anders de vastgelegde marktprijs (onbalans via TenneT, aFRR of day-ahead). Alleen als laatste redmiddel het daggemiddelde; een dag met meer dan 2 uur daggemiddelde gaat als `invalid` mee.

## Prijzen

Afgerekend wordt op de settlementprijs per kwartier. Plugin en integratie bepalen die zoals het BE-dashboard (Balansenergie All-in):

1. **BE stelt vast:** had de accu activiteit in een kwartier (actie of aFRR), dan levert BE na afloop de prijs van dat kwartier. Die gaat altijd voor.
2. **Anders Tenergy:** zonder BE-prijs wordt de onbalansprijs na afloop van het kwartier bepaald uit de minuutprijzen van Tenergy, met de regeltoestand uit de activatiedata (alleen afregelen: laagste prijs; alleen opregelen: hoogste; beide: afname hoogste, teruglevering laagste).
3. **Niet vastgesteld:** de activatiedata staat maar 30 minuten op Tenergy. Een kwartier dat niemand live heeft gevolgd, krijgt alleen de prijs van de laatste minuut. Die telt mee in de berekening, maar niet als vastgesteld.
4. **Na afloop van de dag: TenneT.** TenneT publiceert per dag de tekort- en overschotprijs van elk kwartier ([settlement prices](https://www.tennet.eu/nl-en/settlement-prices-table), openbaar, zonder sleutel of cookie: werkt ook in Safari). Getoetst tegen BE's afrekening: tot op de cent gelijk. Voor afgelopen dagen vervangt die elke niet-vastgestelde of ontbrekende onbalansprijs; day-ahead- en aFRR-prijzen van BE blijven staan. Daarmee is ook de historie volledig vastgesteld geprijsd. De historie verstuurt een maand alleen als TenneT's prijzen voor alle dagen binnen zijn; anders gaat er niets naar Mijnbatterij en staat de reden in het logboek. Per maand toont de plugin een tabel met de gebruikte TenneT-prijzen en een link per dag naar TenneT.

De plugin leest de Tenergy-cache van All-in; de integratie haalt Tenergy zelf elke minuut op.

Per dag staat in het label `kwal`: het aandeel van de dag met eigen meting én vastgestelde prijs. Een versie met lagere `kwal` vervangt nooit een betere, en telt niet mee voor de heartbeat. Zo wint vanzelf de bron die de dag het best heeft gevolgd: de plugin als het dashboard open stond, HA als die doorliep.

## Samenwerking met de browserplugin

Beide zetten achter de notitie van elk dagrecord een label:

```
[BEMB v=1.2.2 bron=ha|browser type=be|cache dekking=99.8 kwal=97.9 actief=2026-11-19T09:30:00+01:00]
```

- **Heartbeat:** Home Assistant zet elke 15 minuten `actief=<tijd>` in één dagrecord.
- **Plugin past zich aan:** ziet de plugin een heartbeat van hooguit 40 minuten oud, dan verstuurt hij geen live meer en vult hij alleen nog dagen aan: dagen die er nog niet staan, of die hij duidelijk nauwkeuriger heeft (bijvoorbeeld van vóór de HA-installatie). De pagina toont dan "Home Assistant verstuurt". Is de heartbeat ouder, dan verstuurt de plugin weer alles zelf.
- **Wie wint per dag:**
  - *definitief* (BE) gaat altijd voor *voorlopig*;
  - zelfde soort vervangt alleen bij minstens gelijke nauwkeurigheid (`kwal`, zie Prijzen; bij definitieve dagen telt ook of de verdeling over huis en net uit eigen metingen komt);
  - zo vult de bron met de meeste meetdata een dag in.
- **Eén live-verzender:** kan Home Assistant zijn heartbeat nergens kwijt (de plugin heeft alle recente dagen nauwkeuriger, bijvoorbeeld vlak na de installatie), dan verstuurt de plugin. Die zet dan elke 10 minuten zelf `actief=<tijd>` in zijn recentste dagrecord, met dezelfde waarden. Zolang die hartslag hooguit 25 minuten oud is, stuurt HA geen live; de status toont dan "browserplugin verstuurt". Sluit je het dashboard, dan neemt HA het binnen 25 minuten over. Zodra HA zelf volledige dagen heeft, wordt HA weer de vaste verzender.
- **Proefstand:** in proefstand stuurt Home Assistant geen heartbeat, dus neemt de plugin niets over.
- **Plugin-override:** in de plugin kun je *Altijd zelf versturen* kiezen.

## Privacy

Je BE-wachtwoord staat in de configuratie van Home Assistant (`.storage/core.config_entries`), net als bij andere integraties met een wachtwoord. De BE-sessie gebruikt een eigen cookie-opslag.

## Ontwikkelen

```
pip install pytest-homeassistant-custom-component
pytest
```

De tests draaien de volledige integratie tegen nagebootste BE- en Mijnbatterij-servers.
