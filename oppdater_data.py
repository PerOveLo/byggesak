# -*- coding: utf-8 -*-
"""
Datapipeline v3 – Byggesaker Kristiansand (hele kommunen).

Arkitektur (kompatibel med fremtidig Postgres-plattform):
  data/index.json          tynn indeks over ALLE saker (kartet laster denne først)
  data/chunks/<postnr>.json  fulle saksobjekter per postnummer (lastes ved behov)
  data/adresser_4204.json  Kartverket-adressecache for hele kommunen
  data/journal_bulk.jsonl  lokal journal-høsting (gitignored, kun backfill)

Kilder:
  * OpenGov: sakene enumereres via sammenhengende kilde-ID-er (~200100 → nå).
    ID-rommet er 1:1 med kommunens saksnummerserie fra 2020 – komplett dekning.
  * Offentlig journal: journaldato per dokument. Backfill = månedsvis bulk-høsting;
    daglig = endringsfeed. (Journalen struper parallell trafikk – alltid sekvensiell.)
  * Kartverket: adresser/koordinater/gnr/bnr.

Kjøring:
  py -X utf8 oppdater_data.py                 daglig inkrementell (nye ID-er + endringsfeed)
  py -X utf8 oppdater_data.py --kristiansand  backfill av hele arkivet (~3-4 t)
  py -X utf8 oppdater_data.py --journal-bulk  månedsvis journal-høsting (~6-8 t, resumerbar)
  py -X utf8 oppdater_data.py --journal-match koble bulk-høstet journal til sakene (lokal, rask)

Ren Python, ingen AI/tokens.
"""

import concurrent.futures
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = "https://opengov.360online.com"
SITE = f"{BASE}/Cases/KRSANDEBYGG"
PJ_BASE = "https://kristiansand.pj.360online.com"
PJ_DEPTS = "18,19,72,74,83"   # Byggesak, Byggesaksbehandling, Plan og bygg, PoB stab, Tilsyn/ulovlighet
KARTVERKET = "https://ws.geonorge.no/adresser/v1/sok"
KOMMUNENUMMER = "4204"

TYPE_BY_PREFIX = {"BYGG": "Byggesak", "HENV": "Henvendelse", "ULOV": "Ulovlighetssak", "PLAN": "Plansak"}
ID_START = 200100            # første sak i arkivet ligger like over (sak 20/00001 ≈ 200157)
EMPTY_RUN_STOP_BACKFILL = 300
EMPTY_RUN_STOP_DAILY = 30
JOURNAL_START = (2020, 1)

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CHUNK_DIR = os.path.join(DATA_DIR, "chunks")
INDEX_JSON = os.path.join(DATA_DIR, "index.json")
LEGACY_CASES = os.path.join(DATA_DIR, "cases.json")
SUMMARIES_JSON = os.path.join(DATA_DIR, "summaries.json")
ADDR_CACHE = os.path.join(DATA_DIR, f"adresser_{KOMMUNENUMMER}.json")
JB_PATH = os.path.join(DATA_DIR, "journal_bulk.jsonl")
JB_WM = os.path.join(DATA_DIR, "journal_watermark.json")

REQUEST_DELAY = 0.15
JOURNAL_DELAY = 0.5
MAX_WORKERS = 3
HEADERS = {"User-Agent": "ByggesakerKristiansand/3.0 (innsynsverktoy; kontakt: konto@vizbo.no)"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url, retries=3):
    last_err = None
    timeout = 90 if "pj.360online.com" in url else 40
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 500:
                return ""  # 360online: 500 = tomt søk
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    log(f"  FEIL: {url}: {last_err}")
    return None


# ---------------------------------------------------------------------------
# Kartverket: alle adresser i kommunen
# ---------------------------------------------------------------------------

def load_addresses(force=False):
    if not force and os.path.exists(ADDR_CACHE):
        if (time.time() - os.path.getmtime(ADDR_CACHE)) / 86400 < 30:
            with open(ADDR_CACHE, encoding="utf-8") as f:
                return json.load(f)
    log(f"Henter alle adresser i kommune {KOMMUNENUMMER} fra Kartverket ...")
    # API-et er begrenset til 10 000 treff per søk – hent per postnummer (46xx).
    adresser = []
    for pnr in range(4600, 4700):
        side = 0
        while True:
            url = (f"{KARTVERKET}?kommunenummer={KOMMUNENUMMER}&postnummer={pnr:04d}"
                   f"&treffPerSide=1000&side={side}&asciiKompatibel=false")
            raw = fetch(url)
            if raw is None:
                break
            payload = json.loads(raw)
            batch = payload.get("adresser", [])
            adresser.extend(batch)
            total = payload.get("metadata", {}).get("totaltAntallTreff", 0)
            if not batch or (side + 1) * 1000 >= total:
                break
            side += 1
            time.sleep(REQUEST_DELAY)
        time.sleep(0.05)
    slim = []
    for a in adresser:
        pt = a.get("representasjonspunkt") or {}
        slim.append({
            "adressetekst": a.get("adressetekst", ""),
            "adressenavn": a.get("adressenavn", ""),
            "gnr": a.get("gardsnummer"), "bnr": a.get("bruksnummer"),
            "lat": pt.get("lat"), "lon": pt.get("lon"),
            "postnr": a.get("postnummer") or "",
        })
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ADDR_CACHE, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)
    log(f"  {len(slim)} adresser.")
    return slim


def build_lookups(addresses):
    by_addr, by_gnrbnr, gnrbnr_addr, streets = {}, {}, {}, {}
    for a in addresses:
        lat, lon, pnr = a["lat"], a["lon"], a["postnr"]
        key = a["adressetekst"].strip().lower()
        if lat is not None and lon is not None:
            by_addr[key] = (lat, lon, pnr)
            streets.setdefault(a["adressenavn"].strip().lower(), []).append((lat, lon))
        if a["gnr"] is not None and a["bnr"] is not None:
            gb = f"{a['gnr']}/{a['bnr']}"
            if lat is not None:
                by_gnrbnr.setdefault(gb, (lat, lon, pnr))
            gnrbnr_addr.setdefault(gb, a["adressetekst"].strip())
    street_centroids = {s: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                        for s, pts in streets.items()}
    return by_addr, by_gnrbnr, gnrbnr_addr, street_centroids


# ---------------------------------------------------------------------------
# OpenGov: detaljside-parsing (ID-enumerering trenger ikke søkesider)
# ---------------------------------------------------------------------------

H2_RE = re.compile(r'<h2>\s*([A-ZÆØÅ]+)-(\d{2}/\d+)\s*-\s*(.*?)\s*</h2>', re.S)
SAKSBEH_RE = re.compile(r'<span>Saksbehandler</span>.*?<p>(.*?)</p>', re.S)
ADDR_LI_RE = re.compile(r'<li>\s*\d+\.\s*(.*?)</li>', re.S)
DOC_RE = re.compile(r'<div class="accordionTitle">\s*<h4>(.*?)</h4>.*?<div id="\d+" class="panel">(.*?)(?=</li>)', re.S)
DOCFIELD_RE = re.compile(r"<div class='documentDetailHeader'><span>(.*?)</span></div>"
                         r"<div class='documentDetailContent'><p>(.*?)</p></div>", re.S)
FILE_RE = re.compile(r'href="(/Cases/KRSANDEBYGG/File/Details/[^"]+)"[^>]*>.*?<div class="fileNameDetail">(.*?)</div>', re.S)
GNRBNR_RE = re.compile(r'\b(\d{1,3})\s*/\s*(\d{1,4})(?:\s*/\s*\d+)*\b')


def parse_detail_page(html):
    """Full parsing av en detaljside. Returnerer None hvis siden ikke er en sak."""
    m = H2_RE.search(html or "")
    if not m:
        return None
    prefix, nr, title = m.group(1), m.group(2), htmllib.unescape(m.group(3)).strip()
    d = {"prefix": prefix, "casenr": f"{prefix}-{nr}", "title": title,
         "saksbehandler": "", "addresses": [], "documents": []}
    sm = SAKSBEH_RE.search(html)
    if sm:
        d["saksbehandler"] = htmllib.unescape(sm.group(1)).strip()
    aside = html.split("caseDetailsAside", 1)
    if len(aside) > 1:
        d["addresses"] = [htmllib.unescape(a).strip() for a in ADDR_LI_RE.findall(aside[1])]
    for dt, panel in DOC_RE.findall(html):
        doc = {"title": htmllib.unescape(dt).strip(), "type": "", "from": "", "to": "", "files": []}
        for k, v in DOCFIELD_RE.findall(panel):
            k, v = htmllib.unescape(k).strip(), htmllib.unescape(v).strip()
            if k == "Dokumenttype":
                doc["type"] = v
            elif k == "Avsender":
                doc["from"] = v
            elif k == "Mottaker":
                doc["to"] = v
        for href, fname in FILE_RE.findall(panel):
            doc["files"].append({"url": BASE + htmllib.unescape(href), "name": htmllib.unescape(fname).strip()})
        d["documents"].append(doc)
    return d


def doc_seq(t):
    m = re.match(r'[A-ZÆØÅ]+-\d{2}/\d+-(\d+)', t or "")
    return int(m.group(1)) if m else 0


def extract_gnrbnr(text):
    m = GNRBNR_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


# ---------------------------------------------------------------------------
# Offentlig journal
# ---------------------------------------------------------------------------

JROW_TITLE_RE = re.compile(r'document-card-title">\s*([^<]*)', re.S)
JROW_DATE_RE = re.compile(r'Journaldato</span></div>\s*<div[^>]*>\s*<p>(.*?)</p>', re.S)


def norm_title(t):
    t = htmllib.unescape(t or "")
    t = re.sub(r'^\s*\d{10}-\d+\s*', '', t)
    t = re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', t)
    t = re.sub(r'[^a-z0-9æøå]+', ' ', t.lower())
    return re.sub(r'\s+', ' ', t).strip()


def parse_journal_rows(html):
    rows = []
    if not html:
        return rows
    for chunk in html.split('<li id="documentrow_')[1:]:
        rid = chunk.split('"', 1)[0]
        m = re.match(r'(\d{4})-(\d+)-(\d+)$', rid)
        if not m:
            continue
        tm, dm = JROW_TITLE_RE.search(chunk), JROW_DATE_RE.search(chunk)
        date_iso = None
        if dm:
            d2 = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', dm.group(1).strip())
            if d2:
                date_iso = f"{d2.group(3)}-{d2.group(2)}-{d2.group(1)}"
        raw = htmllib.unescape(re.sub(r'\s+', ' ', tm.group(1))).strip() if tm else ""
        rows.append({"key": f"{m.group(1)}-{m.group(2)}", "docnum": int(m.group(3)),
                     "date": date_iso, "ntitle": norm_title(raw)})
    return rows


def fetch_journal_case(year, seq, docnum, max_pages=40):
    out, offset = [], 0
    for _ in range(max_pages):
        rows = parse_journal_rows(fetch(
            f"{PJ_BASE}/Journal/SearchRelated?caseYear={year}&sequenceNumber={seq}"
            f"&documentNumber={docnum}&offset={offset}"))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 10:
            break
        offset += 10
        time.sleep(JOURNAL_DELAY)
    return out


def discover_journal_key(case):
    my_titles = {norm_title(d["title"]) for d in case["documents"]} - {""}
    if not my_titles:
        return None
    queries, tried = [], 0
    for d in sorted(case["documents"], key=lambda x: doc_seq(x["title"]), reverse=True):
        t = re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', d["title"]).strip()
        if len(t) >= 12:
            queries.append(t)
    queries.append(case["title"])
    for q in queries:
        if tried >= 3:
            break
        tried += 1
        rows = parse_journal_rows(fetch(f"{PJ_BASE}/Journal/SearchSimple?searchstring={urllib.parse.quote(q[:120])}"))
        time.sleep(JOURNAL_DELAY)
        for r in rows:
            if r["ntitle"] in my_titles:
                y, s = r["key"].split("-")
                return (int(y), int(s), r["docnum"])
    return None


def apply_journal_entries(case, entries):
    """Sett datoer på dokumentene fra journal-rader tilhørende sakens journalKey."""
    tmap = {}
    for r in sorted(entries, key=lambda x: x["docnum"]):
        if r["date"]:
            tmap.setdefault(r["ntitle"], []).append(r["date"])
    hits = 0
    for doc in sorted(case["documents"], key=lambda d: doc_seq(d["title"])):
        nt = norm_title(doc["title"])
        if nt in tmap and tmap[nt]:
            doc["date"] = tmap[nt].pop(0)
            hits += 1
        else:
            doc.setdefault("date", None)
    dates = [d["date"] for d in case["documents"] if d.get("date")]
    case["firstDate"] = min(dates) if dates else None
    case["lastDate"] = max(dates) if dates else None
    return hits


def enrich_case_dates(case, jinfo=None):
    if jinfo is None:
        jinfo = discover_journal_key(case)
    if jinfo is None:
        case["journalKey"] = None
        return False
    year, seq, docnum = jinfo
    entries = fetch_journal_case(year, seq, docnum)
    if not entries:
        case["journalKey"] = None
        return False
    hits = apply_journal_entries(case, entries)
    case["journalKey"] = f"{year}-{seq}"
    case["journalDoc"] = docnum
    return hits > 0


def journal_change_feed(days):
    tod = datetime.now().strftime("%d.%m.%Y")
    fromd = (datetime.now() - timedelta(days=days)).strftime("%d.%m.%Y")
    out, offset = [], 0
    while offset <= 5000:
        rows = parse_journal_rows(fetch(
            f"{PJ_BASE}/Journal/SearchSimple?searchstring=&daterange=custom"
            f"&fromdate={fromd}&todate={tod}&selecteddepartments={PJ_DEPTS}&offset={offset}"))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 10:
            break
        offset += 10
        time.sleep(JOURNAL_DELAY)
    return out


# ---------------------------------------------------------------------------
# Status, søker og oppsummering
# ---------------------------------------------------------------------------

STATUS_RULES = [
    (r'ferdigattest', True, "Avsluttet – ferdigattest gitt"),
    (r'midlertidig brukstillatelse', True, "Midlertidig brukstillatelse gitt"),
    (r'sak(en)? avsluttes|avslutning av sak|avsluttet uten|saken er avsluttet', False, "Avsluttet"),
    (r'trukket|trekker søknad', False, "Søknad trukket"),
    (r'avslag|avslås', True, "Avslag"),
    (r'avvist|avvisning', True, "Avvist"),
    (r'stoppordre', True, "Stoppordre gitt"),
    (r'igangsettingstillatelse|tillatelse til igangsetting', True, "Igangsettingstillatelse gitt"),
    (r'rammetillatelse gitt|rammetillatelse -|- rammetillatelse', True, "Rammetillatelse gitt"),
    (r'tillatelse til tiltak|godkjent søknad|vedtak om tillatelse|dispensasjon innvilget|godkjent dispensasjon|- tillatelse|tillatelse -', True, "Tillatelse gitt"),
    (r'overtredelsesgebyr', True, "Overtredelsesgebyr ilagt"),
    (r'tvangsmulkt', True, "Tvangsmulkt"),
    (r'pålegg om', True, "Pålegg gitt"),
    (r'politisk vedtak|vedtak armu', False, "Politisk vedtak fattet"),
    (r'forhåndsvarsel', True, "Forhåndsvarsel sendt"),
    (r'mangel|etterspør dokumentasjon|mangler ved søknad|ber om flere opplysninger', True, "Venter på tilleggsdokumentasjon"),
    (r'foreløpig svar', True, "Under behandling"),
]
OUTGOING_TYPES = ("dokument ut", "internt notat uten oppfølging", "internt notat",
                  "vedtaksbrev", "mangelbrev - send saken tilbake", "oversendelsesbrev")
INTERNAL_RE = re.compile(r'byggesak|plan og bygg|oppmåling|eiendom|kristiansand kommune|tilsyn|'
                         r'byantikvar|statsforvalter|parkvesen|by- og stedsutvikling|innbygger|stab', re.I)


def classify_vedtaksbrev(title):
    t = title.lower()
    for pat, status in ((r'avslag|avslås', "Avslag"), (r'stoppordre', "Stoppordre gitt"),
                        (r'avvis', "Avvist"), (r'ferdigattest', "Avsluttet – ferdigattest gitt"),
                        (r'igangsetting', "Igangsettingstillatelse gitt"),
                        (r'rammetillatelse', "Rammetillatelse gitt"),
                        (r'godkjent|tillatelse|dispensasjon|innvilg', "Tillatelse gitt")):
        if re.search(pat, t):
            return status
    return "Vedtak fattet"


def infer_status(documents):
    for doc in sorted(documents, key=lambda d: doc_seq(d["title"]), reverse=True):
        t, dtype = doc["title"].lower(), doc["type"].lower()
        if dtype == "vedtaksbrev":
            return classify_vedtaksbrev(doc["title"])
        outgoing = dtype in OUTGOING_TYPES
        for pattern, needs_out, status in STATUS_RULES:
            if re.search(pattern, t) and (not needs_out or outgoing):
                return status
    return "Under behandling" if documents else "Ingen offentlige dokumenter"


def case_soker(case):
    r, fallback = None, None
    for d in sorted(case.get("documents") or [], key=lambda x: doc_seq(x.get("title"))):
        f = (d.get("from") or "").strip()
        if not f or INTERNAL_RE.search(f):
            continue
        dtype = (d.get("type") or "").strip()
        title = (d.get("title") or "").lower()
        if dtype == "Søknad":
            r = f
        elif fallback is None and "dokument inn" in dtype.lower() and "søknad om" in title and \
                not re.search(r'kommentar|merknad|uttalelse|tilsvar|svar på|nabo|klage', title):
            fallback = f
    return r or fallback


# Taksonomi v1 (tiltakstype, regelbasert – LLM-forfining kommer i plattformfasen).
# Rekkefølgen er signifikant: mest spesifikke først. Maks 3 kategorier per sak.
TILTAK_REGLER = [
    ("basseng", r'basseng'),
    ("brygge/sjøbod", r'brygge|sjøbod|båthus|molo|skibbu|naust'),
    ("garasje/carport", r'garasje|carport'),
    ("bod/uthus", r'\bbod\b|\bboder\b|uthus|anneks|drivhus|redskapshus'),
    ("tilbygg/påbygg", r'tilbygg|påbygg|takopplett|\bkvist\b|takoppløft'),
    ("fritidsbolig", r'fritidsbolig|hytte\b'),
    ("bolig (nybygg)", r'enebolig|tomannsbolig|rekkehus|boligbygg|leilighetsbygg|firemannsbolig|nytt bygg.*bolig'),
    ("bruksendring", r'bruksendring'),
    ("fasadeendring", r'fasadeendring|fasade-|vindu(er)? og dør'),
    ("terrasse/veranda", r'terrasse|platting|veranda|balkong|altan'),
    ("mur/gjerde/levegg", r'støttemur|forstøtningsmur|\bmur\b|gjerde|levegg|innhegning'),
    ("riving", r'riving|\brive\b|rivning'),
    ("vei/avkjørsel/parkering", r'avkjørsel|adkomstvei|parkeringsplass|\bvei\b|veianlegg'),
    ("VA-anlegg", r'\bva\b|va-anlegg|avløps|vann- og avløp|vannledning|septik|slamavskiller'),
    ("terrenginngrep", r'terrenginngrep|sprengning|utgraving|planering|(opp)?fylling|masseuttak'),
    ("skilt/reklame", r'skilt|reklame'),
    ("solenergi", r'solcell|solfanger'),
    ("pipe/ildsted", r'\bpipe\b|ildsted|skorstein'),
    ("deling/seksjonering", r'fradeling|deling av|seksjonering|grensejustering|arealoverføring'),
    ("dispensasjon", r'dispensasjon'),
]
_TILTAK_COMPILED = [(navn, re.compile(pat, re.I)) for navn, pat in TILTAK_REGLER]


def klassifiser(case):
    """Regelbasert tiltakstype-klassifisering fra tittel + dokumenttitler."""
    hay = case.get("title", "") + " " + " ".join(d["title"] for d in (case.get("documents") or [])[:6])
    kats = [navn for navn, rx in _TILTAK_COMPILED if rx.search(hay)]
    return kats[:3]


PLANFASE_RULES = [
    (r'vedtak av plan|egengodkjen|vedtatt reguleringsplan|sluttbehandling|planvedtak|kunngjøring av vedtatt', "Vedtatt"),
    (r'offentlig ettersyn|høring|merknad til plan', "På høring"),
    (r'førstegangsbehandling|1\. ?gangsbehandling', "Førstegangsbehandling"),
    (r'planprogram|konsekvensutredning', "Planprogram"),
    (r'varsel om oppstart|oppstartsmøte|planinitiativ|kunngjøring om oppstart|igangsatt regulering', "Oppstart"),
]


def plan_fase(case):
    """Utled planfase fra dokumenttitlene (nyeste først)."""
    if case.get("type") != "Plansak":
        return None
    for d in sorted(case.get("documents") or [], key=lambda x: doc_seq(x["title"]), reverse=True):
        t = d["title"].lower()
        for pat, fase in PLANFASE_RULES:
            if re.search(pat, t):
                return fase
    return "Under arbeid"


def fmt_no_date(iso):
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def generate_summary(case):
    desc = case.get("description") or case["title"]
    n = len(case["documents"])
    year = "20" + case["casenr"].split("-")[1][:2] if "-" in case["casenr"] else ""
    parts = [f"{case['type']} fra {year} som gjelder {desc.strip().rstrip('.')}." if year
             else f"{case['type']}: {desc}."]
    if n == 0:
        parts.append("Saken har ingen offentlig tilgjengelige dokumenter i innsynsløsningen.")
    else:
        parts.append(f"Saken har {n} offentlig{'e' if n != 1 else ''} journalført{'e' if n != 1 else ''} dokument{'er' if n != 1 else ''}.")
        docs = sorted(case["documents"], key=lambda d: doc_seq(d["title"]))
        strip_nr = lambda t: re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', t)  # noqa: E731
        fd = f" ({fmt_no_date(docs[0].get('date'))})" if docs[0].get("date") else ""
        ld = f" ({fmt_no_date(docs[-1].get('date'))})" if docs[-1].get("date") else ""
        parts.append(f"Første dokument: «{strip_nr(docs[0]['title'])}»{fd}. Siste dokument: «{strip_nr(docs[-1]['title'])}»{ld}.")
    parts.append(f"Vurdert status: {case['status']}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Geokoding / adressevisning / saksbygging
# ---------------------------------------------------------------------------

def split_title(title):
    if "," in title:
        head, desc = title.split(",", 1)
        return head.strip(), desc.strip()
    return title.strip(), ""


def geocode(case, lookups):
    by_addr, by_gnrbnr, gnrbnr_addr, street_centroids = lookups
    candidates = []
    for a in case.get("detailAddresses", []):
        m = re.match(r'(?:\d+/\d+(?:/\d+)*\s+)?(.+?),\s*\d{4}', a)
        if m:
            candidates.append(m.group(1).strip().lower())
    tm = re.match(r'^(.*?)\s+\d{1,3}\s*/', case["title"])
    if tm:
        candidates.append(tm.group(1).strip().lower())
    candidates = [re.sub(r'^[\s\-–,]+', '', c) for c in candidates]
    for c in candidates:
        if c in by_addr:
            lat, lon, pnr = by_addr[c]
            return (lat, lon), "adresse", pnr
    gb = extract_gnrbnr(case["title"]) or next(
        (extract_gnrbnr(a) for a in case.get("detailAddresses", []) if extract_gnrbnr(a)), None)
    if gb:
        key = f"{gb[0]}/{gb[1]}"
        if key in by_gnrbnr:
            lat, lon, pnr = by_gnrbnr[key]
            return (lat, lon), "gnr/bnr", pnr
    for c in candidates:
        street = re.sub(r'\s+\d+[a-zæøå]?$', '', c).strip()
        if street in street_centroids:
            return street_centroids[street], "gate", None
    return None, None, None


def display_address(case, gnrbnr_addr):
    for a in case.get("detailAddresses", []):
        m = re.match(r'(?:\d+/\d+(?:/\d+)*\s+)?(.+?),', a)
        if m and m.group(1).strip().lower() not in ("ingen adresse",):
            return m.group(1).strip()
    tm = re.match(r'^[\s\-–]*(.+?)\s+\d{1,3}\s*/', case["title"])
    if tm and len(tm.group(1).strip()) > 2:
        return tm.group(1).strip()
    gb = extract_gnrbnr(case["title"])
    if gb and f"{gb[0]}/{gb[1]}" in gnrbnr_addr:
        return gnrbnr_addr[f"{gb[0]}/{gb[1]}"]
    head = re.sub(r'^[\s\-–]*', '', case["title"].split(",")[0])
    head = re.sub(r'\s*\d{1,3}\s*/\s*\d+(\s*/\s*\d+)*\s*$', '', head).strip()
    if head and len(head) > 2:
        return head
    return f"gnr/bnr {gb[0]}/{gb[1]}" if gb else case["casenr"]


def postnr_for(case, geo_pnr):
    for a in case.get("detailAddresses", []):
        m = re.search(r',\s*(\d{4})\s+\S', a)
        if m:
            return m.group(1)
    return geo_pnr or "0000"


def build_case(cid, det, lookups, old_case=None, announce_new=False):
    addr_head, desc = split_title(det["title"])
    gb = extract_gnrbnr(det["title"])
    case = {
        "id": cid,
        "url": f"{SITE}/Case/Details/{cid}",
        "title": det["title"],
        "casenr": det["casenr"],
        "type": TYPE_BY_PREFIX[det["prefix"]],
        "description": desc,
        "addressHead": addr_head,
        "matrikkel": f"{gb[0]}/{gb[1]}" if gb else "",
        "saksbehandler": det["saksbehandler"],
        "detailAddresses": det["addresses"],
        "documents": sorted(det["documents"], key=lambda d: doc_seq(d["title"]), reverse=True),
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if old_case:
        for k in ("journalKey", "journalDoc", "firstDate", "lastDate"):
            if old_case.get(k):
                case[k] = old_case[k]
        # behold gamle dokumentdatoer via tittelmatch
        old_dates = {norm_title(d["title"]): d.get("date") for d in old_case.get("documents") or []}
        for d in case["documents"]:
            if not d.get("date") and old_dates.get(norm_title(d["title"])):
                d["date"] = old_dates[norm_title(d["title"])]
        dates = [d["date"] for d in case["documents"] if d.get("date")]
        if dates:
            case["firstDate"], case["lastDate"] = min(dates), max(dates)
    case["status"] = infer_status(case["documents"])
    case["soker"] = case_soker(case)
    case["kategorier"] = klassifiser(case)
    if case["type"] == "Plansak":
        hay = (case["title"] + " " + " ".join(d["title"] for d in case["documents"][:4])).lower()
        plankat = []
        if re.search(r'områdereguler', hay):
            plankat.append("områderegulering")
        if re.search(r'detaljreguler', hay):
            plankat.append("detaljregulering")
        if re.search(r'kommunedelplan|kommuneplan', hay):
            plankat.append("kommune(del)plan")
        if re.search(r'endring av reguleringsplan|reguleringsendring|mindre endring', hay):
            plankat.append("planendring")
        case["kategorier"] = (plankat or ["plansak"]) + [k for k in case["kategorier"] if k != "dispensasjon"][:2]
        case["planfase"] = plan_fase(case)

    today = datetime.now().strftime("%Y-%m-%d")
    endringer = list((old_case or {}).get("endringer") or [])
    if old_case is None:
        if announce_new:
            endringer.append({"dato": today, "tekst": "Saken dukket opp i kartet"})
    else:
        n_new, n_old = len(case["documents"]), len(old_case.get("documents") or [])
        if n_new > n_old:
            titles = [re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', d["title"])
                      for d in case["documents"][:n_new - n_old]]
            endringer.append({"dato": today, "tekst": f"{n_new - n_old} nye dokument(er): " + "; ".join(titles[:3])})
        if old_case.get("status") and old_case["status"] != case["status"]:
            endringer.append({"dato": today, "tekst": f"Status endret: {old_case['status']} → {case['status']}"})
    case["endringer"] = endringer[-25:]

    (case["latlon"], case["geoSource"], geo_pnr) = geocode(case, lookups)
    case["displayAddress"] = display_address(case, lookups[2])
    case["postnr"] = postnr_for(case, geo_pnr)
    case["summary"] = generate_summary(case)
    return case


# ---------------------------------------------------------------------------
# Lagring: index + chunks per postnummer
# ---------------------------------------------------------------------------

def thin(case):
    e = {"i": case["id"], "c": case["casenr"], "t": case["type"][0],  # B/H/U
         "s": case.get("status") or "", "a": case.get("displayAddress") or "",
         "p": case.get("postnr") or "0000",
         "n": len(case.get("documents") or []),
         "d": (case.get("description") or "")[:90]}
    if case.get("latlon"):
        e["ll"] = [round(case["latlon"][0], 6), round(case["latlon"][1], 6)]
    for src, dst in (("firstDate", "f"), ("lastDate", "l"), ("saksbehandler", "sb"),
                     ("soker", "so"), ("journalKey", "jk"), ("matrikkel", "m")):
        if case.get(src):
            e[dst] = case[src]
    if case.get("kategorier"):
        e["k"] = case["kategorier"]
    if case.get("planfase"):
        e["pf"] = case["planfase"]
    if case.get("aiSummary"):
        e["ai"] = 1
    if case.get("endringer"):
        nyd = next((x["dato"] for x in case["endringer"] if "dukket opp" in x.get("tekst", "")), None)
        if nyd:
            e["ny"] = nyd
    return e


def load_chunks():
    """-> dict postnr -> {id: case}"""
    chunks = {}
    if os.path.isdir(CHUNK_DIR):
        for fn in os.listdir(CHUNK_DIR):
            if fn.endswith(".json"):
                with open(os.path.join(CHUNK_DIR, fn), encoding="utf-8") as f:
                    data = json.load(f)
                chunks[fn[:-5]] = {c["id"]: c for c in data.get("cases", [])}
    if not chunks and os.path.exists(LEGACY_CASES):
        log("Migrerer fra legacy cases.json ...")
        with open(LEGACY_CASES, encoding="utf-8") as f:
            legacy = json.load(f)
        for c in legacy.get("cases", []):
            c.setdefault("soker", case_soker(c))
            if not c.get("displayAddress"):
                c["displayAddress"] = display_address(c, {})
            pnr = c.get("postnr") or postnr_for(c, None)
            c["postnr"] = pnr
            chunks.setdefault(pnr, {})[c["id"]] = c
    return chunks


def save_store(chunks, touched=None, summaries=None):
    os.makedirs(CHUNK_DIR, exist_ok=True)
    summaries = summaries if summaries is not None else load_summaries()
    all_thin = []
    for pnr, cases in sorted(chunks.items()):
        for c in cases.values():
            ai = summaries.get("cases", {}).get(c["casenr"])
            if ai:
                c["aiSummary"] = ai
            elif "aiSummary" in c:
                del c["aiSummary"]
        all_thin.extend(thin(c) for c in cases.values())
        if touched is None or pnr in touched:
            with open(os.path.join(CHUNK_DIR, f"{pnr}.json"), "w", encoding="utf-8") as f:
                json.dump({"postnr": pnr, "cases": sorted(cases.values(), key=lambda c: c["casenr"], reverse=True)},
                          f, ensure_ascii=False)
    all_thin.sort(key=lambda e: (e.get("l") or "", e["c"]), reverse=True)
    index = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedLocal": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "kommune": "Kristiansand",
        "poiSummaries": summaries.get("pois", {}),
        "cases": all_thin,
    }
    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    return index


def load_summaries():
    if os.path.exists(SUMMARIES_JSON):
        with open(SUMMARIES_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def all_cases(chunks):
    for cases in chunks.values():
        yield from cases.values()


def case_by_id(chunks):
    return {c["id"]: (pnr, c) for pnr, cases in chunks.items() for c in cases.values()}


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------

def generate_ics(chunks):
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    esc_ = lambda t: t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")  # noqa: E731
    events, seen = [], set()
    kw = re.compile(r'befaring|frist|høring|politisk behandling', re.I)
    dre = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{4})\b')
    for c in all_cases(chunks):
        addr = c.get("displayAddress") or c["casenr"]
        if c.get("firstDate") and c["firstDate"] >= cutoff:
            events.append((f"ny-{c['id']}@byggesak", c["firstDate"].replace("-", ""),
                           f"Ny {c['type'].lower()}: {addr}",
                           f"{c.get('description') or c['title']} ({c['casenr']}) – {c['url']}"))
        for d in c.get("documents") or []:
            if not kw.search(d["title"]):
                continue
            for dm in dre.finditer(d["title"]):
                iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
                if iso >= today:
                    events.append((f"dok-{c['id']}-{iso}@byggesak", iso.replace("-", ""),
                                   f"{addr}: {re.sub(r'^[A-ZÆØÅ]+-.{0,12}- *', '', d['title'])[:70]}",
                                   f"{c['casenr']} – {c['url']}"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//byggesaker-kristiansand//NO",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Byggesaker Kristiansand"]
    for uid, dt, summ, desc in events:
        if uid in seen:
            continue
        seen.add(uid)
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{dt}",
                  f"SUMMARY:{esc_(summ)}", f"DESCRIPTION:{esc_(desc)}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(os.path.join(DATA_DIR, "kalender.ics"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    log(f"kalender.ics: {len(seen)} hendelser")


# ---------------------------------------------------------------------------
# Backfill: ID-enumerering av hele arkivet
# ---------------------------------------------------------------------------

def fetch_case_by_id(cid):
    html = fetch(f"{SITE}/Case/Details/{cid}")
    time.sleep(REQUEST_DELAY)
    if html is None:
        return cid, "err", None
    det = parse_detail_page(html)
    if det is None:
        return cid, "empty", None
    return cid, "ok", det


def backfill(chunks, lookups, keep_prefixes=None):
    keep_prefixes = keep_prefixes or set(TYPE_BY_PREFIX)
    known = {c["id"] for c in all_cases(chunks)}
    max_known = max((int(i) for i in known), default=ID_START)
    log(f"Backfill: kjenner {len(known)} saker, høyeste id {max_known}.")

    # Finn øvre grense: probe forbi max til lang tom-serie
    probe = max(max_known + 1, ID_START)
    empty_run, upper = 0, probe
    while empty_run < EMPTY_RUN_STOP_BACKFILL:
        _, status, _ = fetch_case_by_id(probe)
        empty_run = empty_run + 1 if status == "empty" else 0
        if status == "ok":
            upper = probe
        probe += 1
    upper = max(upper, max_known)
    todo = [i for i in range(ID_START, upper + 1) if str(i) not in known]
    log(f"Backfill: øvre grense {upper}, {len(todo)} id-er å hente.")

    summaries = load_summaries()
    processed, kept, touched = 0, 0, set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for cid, status, det in ex.map(fetch_case_by_id, todo):
            processed += 1
            if status == "ok" and det["prefix"] in keep_prefixes:
                case = build_case(str(cid), det, lookups)
                chunks.setdefault(case["postnr"], {})[case["id"]] = case
                touched.add(case["postnr"])
                kept += 1
            if processed % 1000 == 0:
                log(f"  {processed}/{len(todo)} id-er ({kept} saker beholdt) – lagrer")
                save_store(chunks, touched, summaries)
                touched = set()
    save_store(chunks, None, summaries)
    generate_ics(chunks)
    log(f"Backfill ferdig: {processed} id-er, {kept} nye saker, totalt {sum(len(v) for v in chunks.values())}.")


# ---------------------------------------------------------------------------
# Journal-bulk: månedsvis høsting + lokal matching
# ---------------------------------------------------------------------------

def month_range():
    y, m = JOURNAL_START
    now = datetime.now()
    while (y, m) <= (now.year, now.month):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def journal_bulk():
    wm = {"done": []}
    if os.path.exists(JB_WM):
        with open(JB_WM, encoding="utf-8") as f:
            wm = json.load(f)
    months = [ym for ym in month_range() if ym not in wm["done"]]
    log(f"Journal-bulk: {len(months)} måneder igjen.")
    for ym in months:
        y, m = int(ym[:4]), int(ym[5:])
        last_day = ((datetime(y + (m == 12), (m % 12) + 1, 1)) - timedelta(days=1)).day
        fromd, tod = f"01.{m:02d}.{y}", f"{last_day:02d}.{m:02d}.{y}"
        offset, count = 0, 0
        with open(JB_PATH, "a", encoding="utf-8") as out:
            while offset <= 100000:
                rows = parse_journal_rows(fetch(
                    f"{PJ_BASE}/Journal/SearchSimple?searchstring=&daterange=custom"
                    f"&fromdate={fromd}&todate={tod}&selecteddepartments={PJ_DEPTS}&offset={offset}"))
                if not rows:
                    break
                for r in rows:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                count += len(rows)
                if len(rows) < 10:
                    break
                offset += 10
                time.sleep(JOURNAL_DELAY)
        wm["done"].append(ym)
        with open(JB_WM, "w", encoding="utf-8") as f:
            json.dump(wm, f)
        log(f"  {ym}: {count} journalposter")
    log("Journal-bulk ferdig.")


def journal_match(chunks):
    if not os.path.exists(JB_PATH):
        log("Ingen journal_bulk.jsonl – kjør --journal-bulk først.")
        return
    by_key, title_keys = {}, {}
    with open(JB_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_key.setdefault(r["key"], []).append(r)
            if len(r["ntitle"]) >= 15:
                title_keys.setdefault(r["ntitle"], set()).add(r["key"])
    log(f"Journal-match: {len(by_key)} journalsaker, {len(title_keys)} unike titler.")

    matched, ambiguous, unmatched = 0, 0, 0
    touched = set()
    for pnr, cases in chunks.items():
        for c in cases.values():
            if c.get("journalKey") or not c.get("documents"):
                continue
            votes = Counter()
            long_hits = {}
            for d in c["documents"]:
                nt = norm_title(d["title"])
                for k in title_keys.get(nt, ()):  # noqa: B905
                    votes[k] += 1
                    if len(nt) >= 25:
                        long_hits[k] = long_hits.get(k, 0) + 1
            if not votes:
                unmatched += 1
                continue
            best, cnt = votes.most_common(1)[0]
            second = votes.most_common(2)[1][1] if len(votes) > 1 else 0
            ok = (cnt >= 2 and cnt > second) or (cnt == 1 and second == 0 and long_hits.get(best))
            if not ok:
                ambiguous += 1
                continue
            hits = apply_journal_entries(c, by_key[best])
            if hits:
                c["journalKey"] = best
                c["journalDoc"] = by_key[best][0]["docnum"]
                c["summary"] = generate_summary(c)
                matched += 1
                touched.add(pnr)
            else:
                unmatched += 1
    save_store(chunks, touched)
    generate_ics(chunks)
    log(f"Journal-match: {matched} saker datert, {ambiguous} tvetydige, {unmatched} uten treff.")


# ---------------------------------------------------------------------------
# Daglig inkrementell
# ---------------------------------------------------------------------------

def incremental(chunks, lookups):
    byid = case_by_id(chunks)
    known = set(byid)
    max_known = max((int(i) for i in known), default=ID_START)
    touched = set()

    # 1) Nye saker: probe id-er forbi høyeste kjente
    new_ids, probe, empty_run = [], max_known + 1, 0
    while empty_run < EMPTY_RUN_STOP_DAILY:
        cid, status, det = fetch_case_by_id(probe)
        if status == "empty":
            empty_run += 1
        else:
            empty_run = 0
            if status == "ok" and det["prefix"] in TYPE_BY_PREFIX:
                case = build_case(str(cid), det, lookups, None, announce_new=True)
                try:
                    enrich_case_dates(case)
                except Exception as e:  # noqa: BLE001
                    log(f"  journal-feil {case['casenr']}: {e}")
                case["summary"] = generate_summary(case)
                chunks.setdefault(case["postnr"], {})[case["id"]] = case
                touched.add(case["postnr"])
                new_ids.append(case["casenr"])
        probe += 1
    log(f"Inkrementell: {len(new_ids)} nye saker ({', '.join(new_ids[:8])}{'…' if len(new_ids) > 8 else ''}).")

    # 2) Endrede saker via journal-endringsfeed
    feed = journal_change_feed(3)
    key_to_case = {c.get("journalKey"): c["id"] for c in all_cases(chunks) if c.get("journalKey")}
    changed = {key_to_case[e["key"]] for e in feed if e["key"] in key_to_case}
    changed -= {c for c in changed if c not in byid}
    log(f"Endringsfeed: {len(feed)} poster, {len(changed)} kjente saker med aktivitet.")
    for cid in sorted(changed):
        pnr_old, old = byid[cid]
        _, status, det = fetch_case_by_id(int(cid))
        if status != "ok":
            continue
        case = build_case(cid, det, lookups, old)
        if case.get("journalKey"):
            y, s = case["journalKey"].split("-")
            try:
                enrich_case_dates(case, (int(y), int(s), case.get("journalDoc") or 1))
            except Exception as e:  # noqa: BLE001
                log(f"  journal-feil {case['casenr']}: {e}")
        case["summary"] = generate_summary(case)
        if pnr_old != case["postnr"] and old["id"] in chunks.get(pnr_old, {}):
            del chunks[pnr_old][old["id"]]
            touched.add(pnr_old)
        chunks.setdefault(case["postnr"], {})[case["id"]] = case
        touched.add(case["postnr"])

    save_store(chunks, touched)
    generate_ics(chunks)
    log(f"Inkrementell ferdig: {len(new_ids)} nye, {len(changed)} oppdaterte.")


# ---------------------------------------------------------------------------

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    addresses = load_addresses()
    lookups = build_lookups(addresses)
    chunks = load_chunks()
    log(f"Datastore: {sum(len(v) for v in chunks.values())} saker i {len(chunks)} chunks.")

    if "--kristiansand" in sys.argv:
        backfill(chunks, lookups)
    elif "--plansaker" in sys.argv:
        backfill(chunks, lookups, keep_prefixes={"PLAN"})
    elif "--journal-bulk" in sys.argv:
        journal_bulk()
        journal_match(load_chunks())
    elif "--journal-match" in sys.argv:
        journal_match(chunks)
    elif "--reindex" in sys.argv:
        for c in all_cases(chunks):
            if not c.get("kategorier"):
                c["kategorier"] = klassifiser(c)
        save_store(chunks)
        generate_ics(chunks)
        log("Reindeksert (med klassifisering).")
    else:
        incremental(chunks, lookups)


if __name__ == "__main__":
    main()
