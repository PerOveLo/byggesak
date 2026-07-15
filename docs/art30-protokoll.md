# UTKAST: Protokoll over behandlingsaktiviteter (art. 30) – Vizbo AS

> Status: arbeidsutkast 10.07.2026. Følger Datatilsynets mal. Oppdateres ved endringer.

**Behandlingsansvarlig:** Vizbo AS, [org.nr], Industrigata 6, 4614 Kristiansand,
post@vizbo.no. Personvernombud: ikke påkrevd [vurderes ved skalering].

## Aktivitet 1: Publisering av offentlige byggesaksdata

| Felt | Innhold |
|---|---|
| Formål | Allmennhetens innsyn i byggesaksbehandling (kart, søk, statistikk) |
| Rettslig grunnlag | Art. 6(1)(f) – berettiget interesse (se lia-utkast.md) |
| Kategorier registrerte | Saksbehandlere, ansvarlige søkere, personer nevnt i dokumenttitler |
| Kategorier opplysninger | Navn (yrkesrolle/parter), eiendomsadresser, saksmetadata |
| Kilde | Kristiansand kommunes innsynsløsning og offentlige journal (aktivt publisert) |
| Mottakere | Allmennheten |
| Tredjelandsoverføring | GitHub/Cloudflare (US) med SCC – kun offentlige data [flyttes til EU-leverandør i plattformfase] |
| Sletting | Speiler kilden; fjernet hos kommunen → fjernes ved neste synkronisering |
| Sikkerhet | Ingen dokumentinnhold, noindex/robots på rådata, PII-filter med karantene |

## Aktivitet 2: Brukerkontoer og varsling

| Felt | Innhold |
|---|---|
| Formål | Innlogging, følgelister, varsler (e-post/push), private notater |
| Rettslig grunnlag | Art. 6(1)(b) – avtale (brukeren oppretter konto og aktiverer funksjoner) |
| Kategorier registrerte | Tjenestens brukere |
| Kategorier opplysninger | E-post, følgelister (adresser/saker/foretak), notater, push-tokens, innloggingstidspunkt, IP (revisjonslogg) |
| Mottakere | Ingen eksterne; Resend (e-postutsending, databehandler) |
| Tredjelandsoverføring | Cloudflare D1 (US, SCC) [flyttes til Hetzner i plattformfase]; Resend (US, SCC) |
| Sletting | Selvbetjent GDPR-sletting + admin-sletting; sesjoner 90 dager |
| Sikkerhet | Magisk lenke (ingen passord), sesjonstokens, append-only revisjonslogg, rollestyrt admin |

## Aktivitet 3: Revisjonslogg og administrasjon

| Felt | Innhold |
|---|---|
| Formål | Sikkerhet, etterprøvbarhet og dokumentasjonsplikt (art. 5(2)/32) |
| Rettslig grunnlag | Art. 6(1)(f)/(c) |
| Opplysninger | Bruker, handling, tidspunkt, IP, user-agent |
| Sletting | [12–24 mnd – fastsettes]; kan ikke endres/slettes enkeltvis (append-only) |

## Aktivitet 4 (fra betalingslansering): Abonnement og fakturering

| Felt | Innhold |
|---|---|
| Formål | Betalingshåndtering (Vipps Recurring / EHF via regnskapssystem) |
| Rettslig grunnlag | Art. 6(1)(b) avtale + 6(1)(c) bokføringsloven |
| Sletting | Regnskapsdata 5 år (bokføringsloven), adskilt fra bruksdata |
