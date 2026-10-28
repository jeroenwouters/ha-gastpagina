# Gastpagina

Home Assistant-integratie waarmee gasten licht, rolluiken, verwarming en radio
bedienen via de huisgids-pagina, **met hun boekingscode** en zonder
Home Assistant-account. Werkt lokaal en via Nabu Casa. De code werkt alleen
tijdens de boeking, en een gast bereikt alleen de toestellen uit
`gastpagina.yaml`.

Vereist Home Assistant 2026.9 of nieuwer. Geen externe Python-pakketten.

## Hoe het werkt

- Een boeking is een afspraak in een HA-kalender (bv. de Local Calendar "Gasten")
  met in de beschrijving een regel `code: <code>`.
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
   - foute pogingen per 15 minuten per adres (standaard 5).

   Deze instellingen kun je later wijzigen via *Configureren*.

## Een boeking met code toevoegen

Maak in de kalender "Gasten" een afspraak van check-in tot check-out. Zet in
de beschrijving een regel met de code, bv.:

```
Familie Peeters, 2 volwassenen
code: ZEE-4821-MONSTER
```

- Hoofdletters, spaties en streepjes tellen niet: `zee 4821 monster` werkt ook.
- Minstens 10 tekens (zonder spaties en streepjes). Te kort → repair-melding
  "Code te kort voor boeking …" en de code werkt niet.
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
| `sensor.gastpagina_actieve_sessies` | aantal ingelogde toestellen |
| `sensor.gastpagina_foute_pogingen` | foute codes in het laatste uur |
| `switch.gastpagina_toegang` | hoofdschakelaar: uit = gasten kunnen niets (de pagina toont nog de labels) |

Services: `gastpagina.reload` en `gastpagina.revoke_sessions` (zonder veld:
iedereen afmelden; met `booking`: de titel of uid van één afspraak).

Events voor automatiseringen: `gastpagina_login` (zonder gegevens) en
`gastpagina_action` (`device`, `action`). Het logboek toont acties als
"Gastpagina", nooit als een gebruiker.

Bij een blokkade (te veel foute codes vanaf één adres, of 50 in een uur in
totaal: dan is aanmelden 15 minuten dicht) komt er een melding. Diagnostics
(Gastpagina → ⋮ → Diagnostiek downloaden) tonen instellingen, aantallen en de
laatste fouten, zonder codes, tokens, namen of stream-url's.

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

## Testen

Ontwikkelomgeving (Python 3.14):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Testen op het pand na installatie:

1. Maak een testafspraak "Test" van nu tot over een uur met `code: TEST-123456`.
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
| POST | `/api/gastpagina/login` `{code}` → `{token, expires_at}` · 401 · 429 | nee |
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
