# UTKAST: DPIA (personvernkonsekvensvurdering) – Byggesaker Kristiansand

> Status: skjelett/arbeidsutkast 10.07.2026, jf. personvernforordningen art. 35.
> Utløsende faktorer: systematisk behandling i stor skala av offentlig tilgjengelige
> personopplysninger + offentliggjøring + (senere) AI-basert behandling.

## 1. Systematisk beskrivelse av behandlingen

| Element | Beskrivelse |
|---|---|
| Behandlingsansvarlig | Vizbo AS ([org.nr]), Industrigata 6, 4614 Kristiansand |
| Formål | Gjøre offentlige byggesaksdata søkbare, kartfestede og følgbare for allmennheten |
| Kilder | Kommunens innsynsløsning (OpenGov), offentlig journal, Kartverkets adresse-API |
| Kategorier registrerte | Saksbehandlere (ansatte), ansvarlige søkere, parter/naboer nevnt i dokumenttitler, tjenestens egne brukere |
| Kategorier opplysninger | Navn i yrkesrolle, navn i dokumenttitler, adresser/matrikkel (eiendom, ikke person), brukeres e-post/følgelister/notater |
| Mottakere | Allmennheten (offentlige saksdata), ingen tredjepartsdeling av brukerdata |
| Lagring | Saksdata: så lenge kilden publiserer dem. Brukerdata: til konto slettes. Revisjonslogg: [12–24 mnd, fastsettes]. Regnskapsdata: 5 år (bokføringsloven) |
| Databehandlere | GitHub (hosting av offentlige data), Cloudflare (API/D1 – brukerdata), Resend (e-post). [Plattformfase: Hetzner erstatter GitHub/Cloudflare D1 for persondata] |

## 2. Nødvendighet og proporsjonalitet

Se lia-utkast.md. Dataminimering: åpen visning uten privatpersonnavn [plattformfase],
ingen dokumentinnhold, kun metadata + dyplenker. Brukerdata samles kun for funksjoner
brukeren selv aktiverer (følgelister, varsler, notater).

## 3. Risikovurdering

| # | Risiko | S | K | Tiltak |
|---|---|---|---|---|
| 1 | Søkemotorindeksering gjør personnavn googlbare | Høy | Middels | robots.txt + noindex på rådata/persondata; verifiseres kvartalsvis |
| 2 | Sammenstilling/profilering av enkeltpersoner over tid | Middels | Middels | Personsøk kun bak innlogging [plattform]; navnesøk logges i revisjonslogg; AI-laget nekter personspørsmål |
| 3 | Kommunal feilpublisering (fødselsnr., sensitive opplysninger) arves | Lav | Høy | Regex-filter i pipeline + karantenekø; varsling til kommunen ved funn |
| 4 | Brukerdata på avveie (følgelister røper interesse for naboeiendommer) | Lav | Middels | D1 med sesjonsauth, append-only revisjonslogg, GDPR-eksport/-sletting, TLS |
| 5 | Manglende innsigelsesmulighet (Legelisten-kravet) | – | Høy | Skjermingsmekanisme + 30-dagers behandlingsfrist, se lia-utkast.md |
| 6 | Tredjelandsoverføring (Cloudflare/US i MVP) | Middels | Middels | DPA + SCC; [plattformfase: persondata flyttes til EU-eid leverandør (Hetzner), jf. plan] |
| 7 | AI-svar avslører/fabrikkerer personopplysninger [fase 7] | Middels | Høy | Persondata-fritt semantisk lag, maskering før LLM-kall, «vet ikke»-policy, eval-gate |
| 8 | Sikkerhetsbrudd i admin (uautorisert tilgang til brukerlister) | Lav | Høy | Rollebasert tilgang, admin-handlinger i append-only logg, magisk lenke uten passord-lekkasjerisiko |

S = sannsynlighet, K = konsekvens.

## 4. Planlagte tiltak (art. 32)

Append-only revisjonslogg (databasehåndhevet), tilgangsstyring (roller), TLS overalt,
backup med gjenopprettingstest, avviksrutine med 72-timersfrist (art. 33),
databehandleravtaler med alle leverandører, årlig gjennomgang av denne DPIA-en.

## 5. Restpunkter

- [ ] Ferdigstill mottaker-/leverandørliste med DPA-status
- [ ] Fastsett retensjonstid for revisjonslogg
- [ ] Vurder forhåndsdrøfting med Datatilsynet (art. 36) før KRS.no-lansering
- [ ] Oppdater ved AI-lansering (fase 7) og ved flytting til Hetzner-plattform
