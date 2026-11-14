# Gastpagina

Home Assistant-integratie waarmee gasten licht, rolluiken, verwarming en radio
bedienen via de huisgids-pagina, **met hun boekingscode** en zonder
Home Assistant-account. Werkt lokaal en via Nabu Casa. De code werkt alleen
tijdens de boeking, en een gast bereikt alleen de toestellen uit
`gastpagina.yaml`.

Vereist Home Assistant 2026.9 of nieuwer. Geen externe Python-pakketten.

## Hoe het werkt

- Een boeking is een afspraak in een HA-kalender (bv. de Local Calendar "Gasten").
  De integratie zet er zelf een code in, als regel `code: <code>` onderaan de
  beschrijving.
- De pagina (`/local/huisgids.html`) praat met `/api/gastpagina/...` op dezelfde
  HA-server. Na een geldige code krijgt de gast een eigen sessietoken, dat
  meegaat in de header `X-Gast-Token`. Er komt nooit een HA-token in de browser.
- De integratie voert alleen een vaste lijst acties uit op de toestellen uit
  `gastpagina.yaml`. De pagina kent geen entiteit-id's, alleen sleutels zoals
  `living.plafond`.

## Installatie

1. Kopieer `custom_components/gastpagina/` naar `/config/custom_components/`
   (of installeer via HACS als custom repository).
2. Kopieer `config/gastpagina.yaml` naar `/config/gastpagina.yaml` en pas de
   entiteiten aan voor het pand.
3. Kopieer `www/huisgids.html` naar `/config/www/huisgids.html`.
4. Herstart Home Assistant.
5. **Kalender aanmaken**: Instellingen → Apparaten & diensten → Integratie
   toevoegen → *Local Calendar*, naam `Gasten` (entiteit `calendar.gasten`).
6. **Integratie toevoegen**: Instellingen → Apparaten & diensten → Integratie
   toevoegen → *Gastpagina*. Kies de kalender en het venster:
   - code geldig vanaf X uur vóór de start (standaard 24);
   - code geldig tot Y uur na het einde (standaard 0);
   - minimale codelengte (standaard 10);
   - foute pogingen per 15 minuten per adres (standaard 5);
   - codes automatisch aanmaken (standaard aan);
   - voorvoegsel voor codes, max. 6 letters/cijfers (standaard leeg; met `ZM`
     wordt een code `ZM-DUIN-0937-KREEFT`);
   - melding bij een nieuwe code (standaard aan);
   - vooruit kijken: hoeveel dagen vooraf een boeking een code krijgt
     (standaard 90).

   Deze instellingen kun je later wijzigen via *Configureren*.

## Een boeking toevoegen

Maak in de kalender "Gasten" een afspraak **met aankomst- en vertrekuur** (geen
hele-dag-afspraak). Binnen een minuut zet de integratie er een code in, onderaan
de beschrijving:

```
Familie Peeters, 2 volwassenen
code: ZEE-4821-MONSTER
```

De rest van de afspraak blijft zoals ze was. Bij een nieuwe code komt er een
melding in HA ("Code aangemaakt voor Familie Peeters (03-11-2026)"); de code
zelf staat alleen in de afspraak, en in het event `gastpagina_code_created`
(zie verder) om ze door te sturen.

- **Geen code nodig** (poetsdag, eigen gebruik): zet `code: geen` (of `code: -`)
  in de beschrijving. Die afspraak krijgt nooit een code en geeft geen toegang.
- **Hele-dag- of herhalende afspraken** krijgen geen code; je krijgt een
  repair-melding. Geef aankomst- en vertrekuur, of zet `code: geen`.
- **Eigen code**: staat er al een regel `code: …`, dan blijft die. Ze moet
  minstens 10 tekens hebben (zonder spaties en streepjes), anders
  repair-melding "Code te kort voor boeking …" en werkt ze niet.
- **Code gelekt**: service `gastpagina.generate_code` met `booking` (titel of
  uid). De oude code werkt meteen niet meer, gasten van die boeking zijn
  afgemeld, en het antwoord bevat de nieuwe code.
- **Niet wachten**: `gastpagina.fill_codes` vult nu meteen alle ontbrekende
  codes aan (anders gebeurt dat elke minuut).
- Een kalender die niet beschreven kan worden (bv. een iCal-feed) krijgt geen
  automatische codes; je krijgt één repair-melding en eigen codes blijven
  werken.
- Hoofdletters, spaties en streepjes tellen niet: `zee 4821 monster` werkt ook.
- Local Calendar bewaart een gewijzigde afspraak in lokale tijd zonder
  tijdzone-label; het uur blijft hetzelfde. Dat doet ze ook bij elke wijziging
  in de HA-interface.
- Twee overlappende boekingen met dezelfde code werken allebei niet (en geven
  een repair-melding).
- Wijzigingen in de kalender gelden binnen een minuut. Verwijder je de afspraak
  of verander je de code, dan zijn alle sessies van die boeking binnen een
  minuut ongeldig. Verleng je het verblijf, dan blijven ze geldig tot het
  nieuwe einde.

## Toestellen (`gastpagina.yaml`)

Ruimtes met lichten (`light.*` of `switch.*`, `dimmable` voor dimbare
lichten), rolluiken (`cover.*`), één thermostaat per ruimte (`climate.*`), één
radio (`media_player.*`) met zenders, temperatuurgrenzen en scènes. Zie
`config/gastpagina.yaml` voor ’t Zeemonster.

- Per ruimte kan een korte uitleg (`note`, max. 200 tekens), bv. voor iets dat
  niet via de pagina gaat; die staat als kadertje boven de toestellen.
- Na een wijziging: service `gastpagina.reload` (geen herstart nodig).
- Fouten per toestel (verkeerd soort entiteit, dubbele sleutel, ongeldige
  regel) geven een repair-melding; de rest blijft werken. Een entiteit die niet
  bestaat staat op de pagina als "even niet bereikbaar".
- Is het hele bestand onleesbaar, dan blijven de vorige toestellen werken.

## Beheer in Home Assistant

Onder het apparaat **Gastpagina**:

| Entiteit | Betekenis |
|---|---|
| `binary_sensor.gastpagina_boeking_actief` | aan als er nu een geldige boeking is |
| `sensor.gastpagina_actieve_sessies` | aantal ingelogde toestellen van gasten |
| `sensor.gastpagina_eigenaarssessies` | aantal ingelogde toestellen met een eigenaarscode |
| `sensor.gastpagina_foute_pogingen` | foute codes in het laatste uur |
| `switch.gastpagina_toegang` | hoofdschakelaar: uit = gasten kunnen niets (de pagina toont nog de labels); eigenaarscodes blijven werken |

Services: `gastpagina.reload`, `gastpagina.revoke_sessions` (zonder veld:
alle gasten afmelden; met `booking`: de titel of uid van één afspraak; met
`owner: true` ook alle eigenaars), `gastpagina.generate_code` (`booking`),
`gastpagina.fill_codes`, en voor eigenaarscodes `add_owner_code`,
`remove_owner_code` en `list_owner_codes` (zie hieronder).

Events voor automatiseringen: `gastpagina_login` (`role`, en bij eigenaars
`owner`), `gastpagina_action` (`device`, `action`, `role`, en bij eigenaars
`owner`) en `gastpagina_code_created` (`booking`, `start`, `end`, `code`).
`role` is `guest` of `owner`. Het logboek toont acties als "Gastpagina" of
"Gastpagina (eigenaar Jeroen)", nooit als een gebruiker.

## Eigenaarscode instellen

Met een eigenaarscode kun je de bediening altijd gebruiken: ook zonder boeking,
voor en na een verblijf, en ook als `switch.gastpagina_toegang` uit staat (zo
kun je testen terwijl gasten buitengesloten zijn). Het telt niet als boeking:
`binary_sensor.gastpagina_boeking_actief` verandert niet. Je kunt precies
hetzelfde als een gast, met dezelfde grenzen (16–22 °C, maximaal volume).

1. Ontwikkelhulpmiddelen → **Acties** → `gastpagina.add_owner_code`.
2. Vul een **naam** in (bv. `Jeroen`) en laat **code** leeg.
3. Klik op *Actie uitvoeren*. Het antwoord bevat de nieuwe code, bv.
   `DUIN-0937-KREEFT`. **Bewaar ze meteen in je wachtwoordbeheerder**: ze
   wordt alleen gehasht bewaard en is daarna nergens meer op te vragen.
4. Vul de code in op de pagina. Bovenaan staat dan het label "Eigenaar".

- Zelf een code kiezen kan ook (veld **code**, minstens de ingestelde
  codelengte, niet gelijk aan een boekingscode of de code van een andere
  eigenaar).
- Dezelfde naam opnieuw toevoegen geeft die eigenaar een nieuwe code; de
  oude werkt meteen niet meer.
- `gastpagina.remove_owner_code` (naam) verwijdert een code en meldt die
  eigenaar meteen af. `gastpagina.list_owner_codes` toont namen,
  aanmaakdatum en laatst gebruikt, nooit de codes. De namen staan ook bij
  Gastpagina → *Configureren*.
- Een eigenaar blijft aangemeld tot 30 dagen na het laatste gebruik.
- Foute codes tellen mee voor de blokkade, net als bij gasten.
- Een boeking waarvan de code toevallig gelijk is aan een eigenaarscode,
  werkt niet (repair-melding); de automatische codes vermijden dat zelf.

Bij een blokkade (te veel foute codes vanaf één adres, of 50 in een uur in
totaal: dan is aanmelden 15 minuten dicht) komt er een melding. Diagnostics
(Gastpagina → ⋮ → Diagnostiek downloaden) tonen instellingen, aantallen en de
laatste fouten, zonder codes, tokens, namen of stream-url's.

## De code doorsturen

`gastpagina_code_created` is de enige plek buiten de kalender waar de code
staat. Deze automatisering stuurt elke nieuwe code naar de app van de eigenaar
(vervang `mobile_app_mijn_telefoon` door je eigen notify-dienst):

```yaml
alias: Gastpagina – nieuwe code naar mijn telefoon
mode: queued
triggers:
  - trigger: event
    event_type: gastpagina_code_created
actions:
  - action: notify.mobile_app_mijn_telefoon
    data:
      title: "Code voor {{ trigger.event.data.booking }}"
      message: >-
        {{ trigger.event.data.code }}
        ({{ as_timestamp(trigger.event.data.start) | timestamp_custom('%d-%m %H:%M') }}
        tot {{ as_timestamp(trigger.event.data.end) | timestamp_custom('%d-%m %H:%M') }})
```

Let op:

- HA bewaart events in zijn database, ook dit event met de code. Wil je dat
  niet, sluit het uit (de automatisering werkt gewoon verder):

  ```yaml
  recorder:
    exclude:
      event_types:
        - gastpagina_code_created
  ```

- Een pushmelding gaat via de diensten van Apple of Google; een e-mail via je
  mailprovider. Kies wat voor jou veilig genoeg is.
- Zet debug-logging van `homeassistant.core` niet aan: die schrijft alle events
  in het log, ook dit.

## De pagina

- Op `/local/huisgids.html` gebruikt de pagina de echte koppeling. Met `?demo`,
  of geopend buiten Home Assistant, draait een simulatie met voorbeeldpaneel.
- Codescherm onder *Bediening*; het token blijft bewaard op het toestel
  (localStorage). Na het verblijf: "Je verblijf is afgelopen. Tot een volgende
  keer."
- Live updates via Server-Sent Events, met terugval op elke 5 s pollen.
- De QR-code mag naar de Nabu Casa-url wijzen
  (`https://<id>.ui.nabu.casa/local/huisgids.html#bediening`): die werkt binnen
  én buiten.

## Kanalenhub en website (v0.4)

De kalender **Gasten** is de enige bron van waarheid. Airbnb en Booking.com worden elke
10 minuten ingelezen; elk kanaal krijgt één feed terug zonder zijn eigen boekingen.
Requirements: `claude/gastpagina-boekingen-requirements.md` in het project.

### Kanalen koppelen

1. Airbnb → Agenda → Beschikbaarheid → *Agenda's koppelen* → **Agenda exporteren**: kopieer de link.
   Booking.com → Tarieven en beschikbaarheid → *Agenda synchroniseren* → **Exporteren**.
2. Instellingen → Integraties → Gastpagina → **Configureren**: plak beide links, kijk in- en
   uitcheckuur na (16:00 en 10:00) en hoe lang een aanvraag openblijft (48 u).
3. Ontwikkelhulpmiddelen → Acties → `gastpagina.feed_links` geeft de links terug die je
   **in** Airbnb en Booking.com importeert (*Agenda importeren*). Elke link bevat een geheim; deel ze niet.
   Gelekt? `gastpagina.rotate_feed_token` maakt een nieuwe (en de oude werkt niet meer).
4. `gastpagina.sync_now` leest meteen in. Daarna toont `binary_sensor.gastpagina_feed_airbnb_ok`
   of het lukt.

Wat de importer doet: een Airbnb-boeking wordt een afspraak *Airbnb - 4821* van 16:00 tot
10:00 met `bron: airbnb`, `extern: …`, `tel4: …` en `reservatie: …`; de code komt er vanzelf
bij. Gewijzigde data schuiven mee, de code blijft. Verdwijnt een toekomstige boeking uit de
feed, dan gaat de afspraak weg en krijg je een melding. Vallen er ineens meer dan drie weg,
dan gebeurt er niets en komt er een reparatie. Afspraken zonder `bron:`/`extern:` van dat kanaal
raakt de importer nooit aan.

Facebook of telefonisch: maak zelf een afspraak met aankomst- en vertrekuur en zet
`bron: facebook` in de beschrijving. `blokkeert: nee` houdt een afspraak uit de feeds.

Twee boekingen die een nacht delen → kritieke reparatie *Dubbele boeking* en een melding.
Er wordt niets automatisch verwijderd.

### Website

1. De site zit in de integratie (`custom_components/gastpagina/site/`) en komt mee met elke update.
   Een bestand in `/config/gastpagina_site/` met hetzelfde pad gaat voor: zo vervang je teksten of
   pagina's zonder de integratie aan te passen.
2. Zet de foto's in `/config/gastpagina_site/img/` (lijst: `docs/website-fotos.md`). Foto's horen
   niet in de repo.
3. Voeg de sectie `verhuur:` toe aan `gastpagina.yaml` (zie `config/gastpagina.yaml`) en roep
   `gastpagina.reload` aan.
4. De site staat op `https://<nabu-casa>/verhuur/`. Een eigen domein stuur je daarheen door
   (de hoofdpagina van HA blijft de login).

Geen scripts, lettertypes of widgets van derden toevoegen: de site draait op hetzelfde adres
als HA. `/verhuur/` stuurt een strikte Content-Security-Policy mee.

Een aanvraag wordt een **optie** in de kalender (`status: optie`): de nachten zijn meteen
geblokkeerd in de feeds, maar er komt nog geen code. Je beslist met:

- `gastpagina.bevestig` (booking: titel, bv. *An Peeters (website)*) → `status: bevestigd`,
  de code volgt, event `gastpagina_aanvraag_bevestigd`.
- `gastpagina.weiger` (+ optionele `reden`) → afspraak weg, event `gastpagina_aanvraag_geweigerd`.
- Niets doen → na 48 u vervalt de optie (event `gastpagina_aanvraag_vervallen`).

Voorbeeld: melding met knoppen bij een nieuwe aanvraag (pas `notify.mobile_app_…` aan):

```yaml
- alias: Gastpagina – nieuwe aanvraag
  triggers:
    - trigger: event
      event_type: gastpagina_aanvraag
  actions:
    - action: notify.mobile_app_iphone_jeroen
      data:
        title: "Aanvraag: {{ trigger.event.data.naam }}"
        message: >-
          {{ trigger.event.data.personen }} pers., {{ as_datetime(trigger.event.data.aankomst).strftime('%d/%m') }}
          – {{ as_datetime(trigger.event.data.vertrek).strftime('%d/%m') }},
          € {{ trigger.event.data.prijs }}. {{ trigger.event.data.bericht }}
        data:
          actions:
            - action: "GP_OK|{{ trigger.event.data.naam }} (website)"
              title: Bevestigen
            - action: "GP_NEE|{{ trigger.event.data.naam }} (website)"
              title: Weigeren
- alias: Gastpagina – knop op de melding
  triggers:
    - trigger: event
      event_type: mobile_app_notification_action
  conditions:
    - "{{ trigger.event.data.action.startswith('GP_') }}"
  actions:
    - action: "gastpagina.{{ 'bevestig' if trigger.event.data.action.startswith('GP_OK') else 'weiger' }}"
      data:
        booking: "{{ trigger.event.data.action.split('|', 1)[1] }}"
```

Mails aan de gast (ontvangen, bevestigd met betaalgegevens, geweigerd) stuur je met een
automatisering op dezelfde events, bv. met `notify.smtp`.

Privacy: e-mail, telefoon en de laatste 4 cijfers verdwijnen 30 dagen na vertrek uit de
afspraak; de titel wordt *Boeking (website)*.

Nieuwe entiteiten: `sensor.gastpagina_volgende_boeking`, `sensor.gastpagina_openstaande_aanvragen`,
per kanaal `binary_sensor.gastpagina_feed_<kanaal>_ok` en `sensor.gastpagina_feed_<kanaal>_laatste_sync`.

### Website-API

| Methode | Pad | |
|---|---|---|
| GET | `/api/gastpagina/beschikbaarheid?van=&tot=` | bezette nachten (alleen datums) en de regels |
| GET | `/api/gastpagina/prijs?aankomst=&vertrek=&personen=` | indicatieve prijs |
| POST | `/api/gastpagina/aanvraag` | `naam, email, telefoon, personen, aankomst, vertrek, bericht` → `201` / `409 bezet` / `400` / `429` |
| GET | `/api/gastpagina/ical/<token>/<kanaal>.ics` | uitgaande feed (fout token → `404`) |

## Testen

Ontwikkelomgeving (Python 3.14):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Testen op het pand na installatie:

1. Maak een testafspraak "Test" van nu tot over een uur, zonder code. Binnen
   een minuut staat er een code in de beschrijving (of roep
   `gastpagina.fill_codes` aan).
2. Open `/local/huisgids.html#bediening` op een telefoon (wifi én 4G), vul de
   code in en bedien een lamp. Het logboek toont "Gastpagina".
3. Probeer zes keer een foute code: de zesde geeft "Even wachten".
4. Zet `switch.gastpagina_toegang` uit: de bediening staat uit.
5. Verwijder de afspraak: binnen een minuut verschijnt het codescherm weer.

## API

Alle antwoorden JSON met `Cache-Control: no-store`. Zonder geldige sessie:
`401 {"error":"sessie"}`; met toegang uit: `503 {"error":"uit"}`.

| Methode | Pad | Sessie |
|---|---|---|
| POST | `/api/gastpagina/login` `{code}` → `{token, expires_at}` (eigenaar: ook `role: "owner"`) · 401 · 429 · 503 (gastcode met toegang uit) | nee |
| POST | `/api/gastpagina/logout` → 204 | header |
| GET | `/api/gastpagina/config` | nee |
| GET | `/api/gastpagina/state` | ja |
| POST | `/api/gastpagina/stream-ticket` → `{ticket, expires_in}` (30 s, één keer) | ja |
| GET | `/api/gastpagina/stream` (header of `?ticket=`) | ja |
| POST | `/api/gastpagina/action` `{device, action, value?}` → `{ok}` · 400 · 409 | ja |

Acties: lichten `on`/`off`/`toggle`, dimbaar ook `brightness` (1–100);
rolluiken `open`/`close`/`stop`; thermostaat `target` (afgerond en begrensd) en
`power` (`on`/`off`); radio `play` (zendersleutel), `stop`, `volume` (0–1,
begrensd); scènes als `{device: "scene", action: "slaapwel"}`, het antwoord
bevat `previous` voor "ongedaan maken".
