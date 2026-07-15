# UTKAST: Interesseavveining (LIA) – Byggesaker Kristiansand

> Status: arbeidsutkast 10.07.2026. Dette er ikke juridisk rådgivning; utkastet bør
> kvalitetssikres av personvernrådgiver/advokat før kommersiell lansering.
> Rettslig grunnlag: personvernforordningen art. 6 nr. 1 bokstav f (berettiget interesse).

## 1. Behandlingen

Tjenesten samler inn og viser **allerede offentliggjorte** plan- og byggesaksdata fra
Kristiansand kommunes innsynsløsning og offentlige journal: saksnummer, tittel, status,
adresse, matrikkel, dokumenttitler med journaldato, saksbehandlernavn og ansvarlig
søker (foretak). Personopplysninger som inngår: navn på saksbehandlere (yrkesrolle),
navn på ansvarlige søkere (oftest foretak, unntaksvis enkeltpersoner) og
privatpersonnavn som forekommer i dokumenttitler (parter/naboer).

## 2. Berettiget interesse (trinn 1)

- Allmennhetens innsyn i offentlig myndighetsutøvelse (byggesaksbehandling), jf.
  formålet bak offentleglova og Grunnloven § 100 femte ledd.
- Naboers og lokalsamfunnets konkrete behov for å følge byggeaktivitet som berører dem.
- Pressens arbeidsvilkår (tjenesten er tiltenkt integrasjon i lokalavis).
- Interessen er reell, aktuell og lovlig – opplysningene er aktivt publisert av kommunen.

## 3. Nødvendighet (trinn 2)

Formålet kan ikke oppnås like effektivt med mindre inngripende midler: kommunens egen
løsning mangler kart, historikk på tvers av saker, varsling og statistikk. Tjenesten
viser minst mulig persondata (dataminimering, se trinn 3) og lenker til kilden i
stedet for å kopiere dokumentinnhold.

## 4. Avveining mot de registrertes interesser (trinn 3)

**Risiko:** gjenfinnbarhet av privatpersoners navn løsrevet fra kommunens kontekst;
søkemotoreksponering; sammenstilling over tid. Relevant praksis: HR-2021-2403-A
(Legelisten – berettiget interesse aksepterte omtale av yrkesutøvere, med krav om
fungerende innsigelsesmekanisme) og Datatilsynets praksis om at postlister/innsynsdata
ikke skal søkemotorindekseres (jf. overtredelsesgebyr til Asker kommune).

**Avbøtende tiltak (implementert/planlagt):**
1. Privatpersonnavn vises ikke i åpen visning uten innlogging [plattformfase; i MVP
   vises kun det kommunen selv publiserer i titler].
2. `robots.txt` og noindex hindrer søkemotorindeksering av rådata og persondata.
3. Saksbehandler-/foretaksnavn behandles som yrkesrolleopplysninger (lav risiko).
4. Innsigelses- og skjermingsmekanisme: enhver kan kreve navn skjermet
   (post@vizbo.no); skjerming vurderes innen 30 dager og loggføres.
5. Ingen dokumentinnhold republiseres; dyplenker til kommunens innsyn.
6. Fødselsnummer-/sensitivfilter i datapipelinen med karantene for treff.
7. Sletting/oppdatering speiler kilden: fjernes noe hos kommunen, forsvinner det hos
   oss ved neste synkronisering.

**Konklusjon (utkast):** Interessene i åpenhet og etterprøvbarhet veier tyngre enn
ulempene for de registrerte, gitt tiltakene over. Behandlingen kan bygges på art. 6(1)(f).

## 5. Restpunkter før lansering

- [ ] Juridisk kvalitetssikring av denne LIA-en
- [ ] DPIA ferdigstilles (se dpia-utkast.md)
- [ ] Innsigelsesmekanisme får eget skjema i tjenesten (ikke bare e-post)
- [ ] Vurder forhåndsdialog med Datatilsynets veiledningstjeneste
