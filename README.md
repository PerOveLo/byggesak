# Byggesakskart Flekkerøy

Interaktivt kart over byggesaker, henvendelser og ulovlighetssaker på Flekkerøy,
hentet fra [Kristiansand kommunes innsynsløsning](https://opengov.360online.com/Cases/KRSANDEBYGG)
og datert via [kommunens offentlige journal](https://kristiansand.pj.360online.com/).

## Bruk

**Anbefalt:** Dobbeltklikk **`start kart.bat`** → åpner `http://localhost:8742` med lokal server.
Da fungerer innebygd PDF-visning av vedlegg og automatisk sideoppdatering.
(`index.html` kan også åpnes direkte som fil; vedlegg åpnes da i egen fane i stedet.)

- 🔵 **Byggesak** · 🟠 **Henvendelse** · 🔴 **Ulovlighetssak** – tall i markøren = antall saker på punktet
- Klikk markør → panel med analyse, status, dokument-tidslinje **med journaldatoer** og PDF-vedlegg
- **Følg saker** med ☆-knappen: «NYTT»-merke, varselbanner og rød teller på ★ når fulgte saker får nye
  dokumenter. Tillat nettleser-varsler (spørres ved klikk på ★) for systemvarsler.
- Filtrer med sakstype-knappene, årsvelger og søkefelt. På mobil: dra i håndtaket på bunnarket.

## Oppdatering

- **Automatisk:** Planlagt oppgave `flekkeroy-byggesak-oppdatering` (Claude, «Scheduled» i sidepanelet)
  kjører daglig kl. 07. Kjøres ved neste appstart hvis maskinen var av.
- **Manuelt:** Dobbeltklikk `oppdater.bat` (eller `py -X utf8 oppdater_data.py`; `--full` for alt på nytt).

Oppdateringen er **inkrementell**: Ny-saker fanges via hovedlistene, endringer i eksisterende saker via
offentlig journals endringsfeed (avdelingsfiltrert). Detaljer og datoer hentes kun for saker med faktisk
aktivitet. Full gjennomgang av alle gater skjer automatisk maks én gang i uka. Skriptet er ren Python og
bruker ingen AI-tokens; kun den planlagte Claude-oppgaven bruker tokens når den skriver analyser.

## Datakilder og virkemåte

1. **Kartverket** (åpne adresse-API): alle ~2000 adresser i 4625 Flekkerøy → koordinater, gnr/bnr, gatenavn.
   Cache: `data/adresser_4625.json` (30 dager).
2. **OpenGov**: saker søkes per gatenavn + hovedlister; kandidater matches på gate/gnr; detaljsiden
   verifiserer postnummer 4625. (OpenGov svarer HTTP 500 ved null søketreff – normalt.)
3. **Offentlig journal**: journalsak identifiseres via tittelsøk, dokumenter dateres med journaldato
   (= innsendt/utsendt dato). Interne notater journalføres ikke og mangler dato.
4. **Visning**: gnr/bnr oversettes til adresse (`displayAddress`); status utledes av dokumenttitler
   (vedtaksbrev veier tyngst); AI-analyser fra `data/summaries.json` flettes inn (🧠).

## Offentlig hosting (GitHub Pages + Actions)

Repoet er rigget for gratis, serverløs drift:

1. **GitHub-repo**: push dette repoet til GitHub (privat eller offentlig – Pages krever offentlig repo på gratisplan).
2. **Actions**: workflowen `.github/workflows/oppdater-data.yml` kjører `oppdater_data.py` daglig kl. 06 norsk tid
   og committer oppdaterte `data/`-filer. Kan også trigges manuelt fra Actions-fanen.
3. **Pages**: Settings → Pages → «Deploy from a branch» → `main` / `(root)`. Kartet blir liggende på
   `https://<bruker>.github.io/<repo>/`.
4. **E-postvarsling + PDF-proxy** (valgfritt, gratis): deploy `worker/varsler-worker.js` til Cloudflare Workers
   (instruksjoner i filens topp), fyll inn worker-URLen i `config.js`, og legg inn GitHub Secrets:
   `VARSLER_API_URL`, `VARSLER_API_SECRET`, `RESEND_API_KEY` (resend.com), `VARSLER_FRA`, `SITE_URL`.
   Da sender den daglige Actions-kjøringen e-post til alle som har registrert adresser via ★-panelet i kartet.
   Uten worker fungerer alt annet – PDF-er åpnes da i egen fane, og varsling skjer kun i appen.

**Personvern:** Dette re-publiserer offentlige innsynsdata (adresser/navn i dokumenttitler). Vurder å legge
siden bak tilgangskontroll (f.eks. Cloudflare Access) hvis den ikke skal være åpen for alle.
Abonnent-e-poster lagres kun i Cloudflare KV (aldri i repoet). Merk: v1 av «Vis mine varslinger» har ingen
innlogging – hvem som helst som kjenner e-postadressen kan se hvilke adresser den følger.

## Filer

| Fil | Innhold |
|---|---|
| `index.html` | Kartet (Leaflet + PDF.js, lys/mørk modus, mobil/PC) |
| `server.py` | Lokal server + PDF-proxy (OpenGov mangler CORS) |
| `oppdater_data.py` | Innhenting, journaldatoer, geokoding, statusanalyse |
| `data/cases.js` / `cases.json` | Saksdata |
| `data/summaries.json` | AI-/håndskrevne analyser (redigerbar) |
| `start kart.bat` / `oppdater.bat` | Start kart / manuell oppdatering |
