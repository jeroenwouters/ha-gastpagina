# Aanvulling requirements — eigenaarscodes (gastpagina v0.3)

29-10-2026. Bouwt verder op v0.2 (automatische codes), geïnstalleerd op Home Assistant Oostende (`home.hetzeemonster.be`).

## Doel

De eigenaars (Jeroen en partner) kunnen de bediening op de gastpagina altijd gebruiken met een eigen code, ook buiten een boeking, zonder dat dat als boeking telt of automatiseringen beïnvloedt.

## Gedrag

1. **Eigenaarscodes** zijn een aparte lijst, los van de kalender. Elke code heeft een **naam** (bv. "Jeroen", "Partner") en de code zelf.
2. Een eigenaarscode werkt **altijd**: buiten boekingen, voor en na het verblijf van gasten, en **ook als `switch.gastpagina_toegang` uit staat** (zodat de eigenaar kan testen terwijl gasten buitengesloten zijn).
3. Een eigenaarscode telt **niet als boeking**:
   - `binary_sensor.gastpagina_boeking_actief` blijft ongewijzigd;
   - `sensor.gastpagina_actieve_sessies` telt alleen gastsessies; nieuw: `sensor.gastpagina_eigenaarssessies`.
4. Inloggen gaat via hetzelfde codescherm en hetzelfde endpoint (`POST /api/gastpagina/login`). De integratie controleert eerst eigenaarscodes, dan boekingscodes. Zelfde normalisatie (hoofdletters, spaties, streepjes) en constant-time vergelijking.
5. **Sessie**: geldig 30 dagen, verlengd bij elk gebruik (sliding). Bij verwijderen of wijzigen van de eigenaarscode worden alle sessies van die code meteen ingetrokken. `gastpagina.revoke_sessions` zonder velden trekt **alleen gastsessies** in; met `owner: true` ook eigenaarssessies.
6. Rechten: exact dezelfde witte lijst van toestellen en acties als gasten (geen extra toestellen, zelfde grenzen 16–22 °C en max. volume). Eigenaars die meer willen, gebruiken HA zelf.
7. Rate limiting en blokkade gelden ook voor eigenaarscodes (een foute code is een foute code).
8. Antwoord van `/login` bij een eigenaarscode: zelfde vorm, met extra `"role": "owner"` en zonder `expires_at` van een boeking (wel de sessie-vervaldatum). De pagina mag dan bovenaan een klein label tonen: "Eigenaar". De melding "Je verblijf is afgelopen" verschijnt nooit voor een eigenaar.

## Opslag en beheer

- Codes worden **alleen gehasht** bewaard (bv. PBKDF2/scrypt met salt, via `homeassistant.helpers.storage.Store`), nooit leesbaar terug op te vragen.
- **Services** (met `supports_response`):
  - `gastpagina.add_owner_code` — velden `name` (verplicht), `code` (optioneel). Zonder `code` genereert de integratie er een in hetzelfde formaat als boekingscodes (bv. `DUIN-0937-KREEFT`) en geeft die **eenmalig** terug in het antwoord. Met `code`: minimaal de ingestelde codelengte; mag niet gelijk zijn aan een actieve of toekomstige boekingscode.
  - `gastpagina.remove_owner_code` — veld `name`. Trekt de sessies in.
  - `gastpagina.list_owner_codes` — geeft alleen namen + aanmaakdatum + laatst gebruikt terug, nooit de code.
- **Opties** (Gastpagina → Configureren): toon de namen van de eigenaarscodes (alleen-lezen) met uitleg dat beheer via de services gaat. Geen codes in de opties-UI of in `core.config_entries`.
- Een boekingscode die toevallig gelijk is aan een eigenaarscode → de automatische codegenerator mag zo'n code nooit maken; een handmatig ingevulde botsende boekingscode → repair-issue en de boeking werkt niet (eigenaarscode blijft werken).

## Logboek en events

- Acties met een eigenaarscode verschijnen in het logboek als **"Gastpagina (eigenaar Jeroen)"**; gastacties blijven "Gastpagina".
- Events `gastpagina_login` / `gastpagina_action` krijgen een veld `role: guest|owner` en bij eigenaars `owner: <naam>`.
- Codes nooit in logs, events, diagnostics of entiteitsattributen.

## Acceptatietests (bovenop 1–26)

27. `add_owner_code` zonder code → antwoord bevat een gegenereerde code; daarna werkt die code op de pagina.
28. Eigenaarscode werkt zonder actieve boeking; `boeking_actief` blijft `off`.
29. Eigenaarscode werkt met `switch.gastpagina_toegang` uit; een gastcode dan niet (503).
30. `list_owner_codes` toont namen, nooit codes; opslagbestand bevat geen leesbare code.
31. `remove_owner_code` → bestaande eigenaarssessie krijgt binnen één verzoek `401`.
32. `revoke_sessions` zonder velden laat eigenaarssessies bestaan; met `owner: true` niet.
33. Eigenaarssessie verloopt na 30 dagen zonder gebruik; gebruik verlengt.
34. Grenzen gelden ook voor eigenaars (30 °C → 22).
35. Logboek toont "Gastpagina (eigenaar …)"; events hebben `role: owner`.
36. Generator maakt nooit een boekingscode gelijk aan een eigenaarscode.
37. Pagina toont het label "Eigenaar" en nooit "Je verblijf is afgelopen" voor een eigenaarssessie.

## Documentatie

README: sectie "Eigenaarscode instellen" (Ontwikkelhulpmiddelen → Acties → `gastpagina.add_owner_code`, naam invullen, code uit het antwoord bewaren in je wachtwoordbeheerder).
