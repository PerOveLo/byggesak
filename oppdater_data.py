# -*- coding: utf-8 -*-
"""
Oppdaterer byggesaksdata for Flekkerøy.

Kilder:
  * OpenGov (opengov.360online.com/Cases/KRSANDEBYGG): saker + dokumentlister + PDF-lenker
  * Offentlig journal (kristiansand.pj.360online.com): journaldato per dokument
  * Kartverket (ws.geonorge.no): adresser, koordinater, gnr/bnr for 4625 Flekkerøy

Kjøring:
  py -X utf8 oppdater_data.py            inkrementell (journal-endringsfeed + hovedlister;
                                         full gatesveip automatisk hvis >7 dager siden sist)
  py -X utf8 oppdater_data.py --full     hent alt på nytt (inkl. datoberikelse for alle saker)

Skriptet er ren Python (ingen AI/tokens). CPU/nettverk brukes kun på nye/endrede saker
ved inkrementell kjøring.
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
from datetime import datetime, timedelta, timezone

BASE = "https://opengov.360online.com"
SITE = f"{BASE}/Cases/KRSANDEBYGG"
PJ_BASE = "https://kristiansand.pj.360online.com"
# Avdelinger i offentlig journal som dekker plan/bygg/tilsyn:
# 18 Byggesak, 19 Byggesaksbehandling, 72 Plan og bygg, 74 Plan og bygg stab, 83 Tilsyn og ulovlighetsoppfølgning
PJ_DEPTS = "18,19,72,74,83"
KARTVERKET = "https://ws.geonorge.no/adresser/v1/sok"
# Områder som dekkes: postnummer -> områdenavn. Nye områder legges til her;
# skriptet oppdager endringen og kjører automatisk full gatesveip neste gang.
POSTNUMRE = {
    "4625": "Flekkerøy",
    "4637": "Søm",
}
KOMMUNENUMMER = "4204"       # Kristiansand

CASE_TYPES = {
    "Byggesak": "99001",
    "Henvendelse": "99005",
    "Ulovlighetssak": "99004",
}

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CASES_JSON = os.path.join(DATA_DIR, "cases.json")
CASES_JS = os.path.join(DATA_DIR, "cases.js")
SUMMARIES_JSON = os.path.join(DATA_DIR, "summaries.json")
ADDR_CACHE = os.path.join(DATA_DIR, f"adresser_{'_'.join(sorted(POSTNUMRE))}.json")

REQUEST_DELAY = 0.2
JOURNAL_DELAY = 0.6   # journalen struper parallell trafikk – vær skånsom
MAX_WORKERS = 3       # gjelder OpenGov; journalen hentes alltid sekvensielt
SWEEP_INTERVAL_DAYS = 7
HEADERS = {"User-Agent": "FlekkeroyByggesakskart/2.0 (privat innsynsverktoy; kontakt: konto@vizbo.no)"}

FINAL_STATUSES = {
    "Avsluttet – ferdigattest gitt",
    "Avsluttet",
    "Søknad trukket",
    "Avvist",
    "Avslag",
}


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
                return ""  # 360online svarer 500 når et søk har null treff
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    log(f"  FEIL ved henting av {url}: {last_err}")
    return None


# ---------------------------------------------------------------------------
# Kartverket: adresser på Flekkerøy
# ---------------------------------------------------------------------------

def load_area_addresses(force=False):
    if not force and os.path.exists(ADDR_CACHE):
        age_days = (time.time() - os.path.getmtime(ADDR_CACHE)) / 86400
        if age_days < 30:
            with open(ADDR_CACHE, encoding="utf-8") as f:
                return json.load(f)

    slim = []
    for postnr, navn in POSTNUMRE.items():
        log(f"Henter adresser for {postnr} {navn} fra Kartverket ...")
        adresser = []
        side = 0
        while True:
            url = (f"{KARTVERKET}?postnummer={postnr}&kommunenummer={KOMMUNENUMMER}"
                   f"&treffPerSide=1000&side={side}&asciiKompatibel=false")
            raw = fetch(url)
            if raw is None:
                break
            payload = json.loads(raw)
            batch = payload.get("adresser", [])
            adresser.extend(batch)
            total = payload.get("metadata", {}).get("totaltAntallTreff", 0)
            if len(adresser) >= total or not batch:
                break
            side += 1
            time.sleep(REQUEST_DELAY)
        for a in adresser:
            pt = a.get("representasjonspunkt") or {}
            slim.append({
                "adressetekst": a.get("adressetekst", ""),
                "adressenavn": a.get("adressenavn", ""),
                "gnr": a.get("gardsnummer"),
                "bnr": a.get("bruksnummer"),
                "lat": pt.get("lat"),
                "lon": pt.get("lon"),
                "omrade": navn,
            })
        log(f"  {len(adresser)} adresser i {navn}.")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ADDR_CACHE, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)
    return slim


def build_lookups(addresses):
    by_addr = {}      # "skibbuveien 2b" -> (lat, lon)
    by_gnrbnr = {}    # "2/122" -> (lat, lon)
    gnrbnr_addr = {}  # "2/122" -> "Skibbuveien 2B"
    streets = {}
    gnr_set = set()
    for a in addresses:
        lat, lon = a["lat"], a["lon"]
        key = a["adressetekst"].strip().lower()
        if lat is not None and lon is not None:
            by_addr[key] = (lat, lon)
            streets.setdefault(a["adressenavn"].strip().lower(), []).append((lat, lon))
        if a["gnr"] is not None and a["bnr"] is not None:
            gb = f"{a['gnr']}/{a['bnr']}"
            if lat is not None:
                by_gnrbnr.setdefault(gb, (lat, lon))
            gnrbnr_addr.setdefault(gb, a["adressetekst"].strip())
            gnr_set.add(a["gnr"])
    street_centroids = {
        s: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        for s, pts in streets.items()
    }
    street_names = sorted({a["adressenavn"].strip() for a in addresses if a["adressenavn"].strip()})
    return by_addr, by_gnrbnr, gnrbnr_addr, street_centroids, street_names, gnr_set


# ---------------------------------------------------------------------------
# OpenGov: sakslister og detaljsider
# ---------------------------------------------------------------------------

ITEM_RE = re.compile(
    r'<li class="_casefilter">.*?href="(/Cases/KRSANDEBYGG/Case/Details/(\d+))".*?'
    r'<div class="caseName">\s*<span>(.*?)</span>.*?'
    r'<div class="caseDate">\s*<span>(.*?)</span>\s*<span>(.*?)</span>',
    re.S,
)
ADDR_P_RE = re.compile(r'<div class="serachCaseResult">\s*<p>(.*?)</p>', re.S)


def parse_case_list(html):
    items = []
    for chunk in html.split('<li class="_casefilter">')[1:]:
        chunk = '<li class="_casefilter">' + chunk
        m = ITEM_RE.search(chunk)
        if not m:
            continue
        url, cid, title, casenr, ctype = m.groups()
        am = ADDR_P_RE.search(chunk)
        list_addr = htmllib.unescape(am.group(1)).strip() if am else ""
        if list_addr.lower() == "ingen adresse":
            list_addr = ""
        items.append({
            "id": cid,
            "url": BASE + url,
            "title": htmllib.unescape(title).strip(),
            "casenr": htmllib.unescape(casenr).strip(),
            "type": htmllib.unescape(ctype).strip(),
            "listAddress": list_addr,
        })
    return items


GNRBNR_RE = re.compile(r'\b(\d{1,3})\s*/\s*(\d{1,4})(?:\s*/\s*\d+)*\b')


def extract_gnrbnr(text):
    m = GNRBNR_RE.search(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_flekkeroy_candidate(item, street_names_lower, gnr_set):
    addr = item["listAddress"].lower()
    title = item["title"].lower().lstrip("-– ")
    for s in street_names_lower:
        if addr.startswith(s + " ") or addr == s or title.startswith(s + " ") or title.startswith(s + ","):
            return True
    gb = extract_gnrbnr(item["title"])
    if gb and gb[0] in gnr_set:
        return True
    return False


H2_RE = re.compile(r'<div class="pageTitleHeader">\s*<h2>\s*(.*?)</h2>', re.S)
SAKSBEH_RE = re.compile(r'<span>Saksbehandler</span>.*?<p>(.*?)</p>', re.S)
ADDR_LI_RE = re.compile(r'<li>\s*\d+\.\s*(.*?)</li>', re.S)
DOC_RE = re.compile(
    r'<div class="accordionTitle">\s*<h4>(.*?)</h4>.*?<div id="\d+" class="panel">(.*?)(?=</li>)',
    re.S,
)
DOCFIELD_RE = re.compile(
    r"<div class='documentDetailHeader'><span>(.*?)</span></div>"
    r"<div class='documentDetailContent'><p>(.*?)</p></div>",
    re.S,
)
FILE_RE = re.compile(r'href="(/Cases/KRSANDEBYGG/File/Details/[^"]+)"[^>]*>.*?'
                     r'<div class="fileNameDetail">(.*?)</div>', re.S)


def parse_detail(html):
    d = {"saksbehandler": "", "addresses": [], "documents": []}
    m = SAKSBEH_RE.search(html)
    if m:
        d["saksbehandler"] = htmllib.unescape(m.group(1)).strip()
    aside = html.split("caseDetailsAside", 1)
    if len(aside) > 1:
        d["addresses"] = [htmllib.unescape(a).strip() for a in ADDR_LI_RE.findall(aside[1])]
    for title, panel in DOC_RE.findall(html):
        doc = {"title": htmllib.unescape(title).strip(), "type": "", "from": "", "to": "", "files": []}
        for k, v in DOCFIELD_RE.findall(panel):
            k = htmllib.unescape(k).strip()
            v = htmllib.unescape(v).strip()
            if k == "Dokumenttype":
                doc["type"] = v
            elif k == "Avsender":
                doc["from"] = v
            elif k == "Mottaker":
                doc["to"] = v
        for href, fname in FILE_RE.findall(panel):
            doc["files"].append({"url": BASE + htmllib.unescape(href),
                                 "name": htmllib.unescape(fname).strip()})
        d["documents"].append(doc)
    return d


def doc_seq(doc_title):
    m = re.match(r'[A-ZÆØÅ]+-\d{2}/\d+-(\d+)', doc_title)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Offentlig journal: datoberikelse
# ---------------------------------------------------------------------------

JROW_TITLE_RE = re.compile(r'document-card-title">\s*([^<]*)', re.S)
JROW_DATE_RE = re.compile(r'Journaldato</span></div>\s*<div[^>]*>\s*<p>(.*?)</p>', re.S)


def norm_title(t):
    """Normaliser dokumenttittel for kobling OpenGov <-> journal."""
    t = htmllib.unescape(t)
    t = re.sub(r'^\s*\d{10}-\d+\s*', '', t)                      # journalens nummer-token
    t = re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', t)        # OpenGov-prefiks
    t = t.lower()
    t = re.sub(r'[^a-z0-9æøå]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def parse_journal_rows(html):
    """Returnerer [{year, seq, docnum, key, date(ISO), ntitle, rawtitle}]"""
    rows = []
    if not html:
        return rows
    for chunk in html.split('<li id="documentrow_')[1:]:
        rid = chunk.split('"', 1)[0]
        m = re.match(r'(\d{4})-(\d+)-(\d+)$', rid)
        if not m:
            continue
        year, seq, docnum = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tm = JROW_TITLE_RE.search(chunk)
        dm = JROW_DATE_RE.search(chunk)
        date_iso = None
        if dm:
            dm2 = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', dm.group(1).strip())
            if dm2:
                date_iso = f"{dm2.group(3)}-{dm2.group(2)}-{dm2.group(1)}"
        raw = htmllib.unescape(re.sub(r'\s+', ' ', tm.group(1))).strip() if tm else ""
        rows.append({"year": year, "seq": seq, "docnum": docnum, "key": f"{year}-{seq}",
                     "date": date_iso, "ntitle": norm_title(raw), "rawtitle": raw})
    return rows


def fetch_journal_case(year, seq, docnum, max_pages=30):
    """Alle journalførte dokumenter for en journalsak, med dato."""
    out = []
    offset = 0
    for _ in range(max_pages):
        url = (f"{PJ_BASE}/Journal/SearchRelated?caseYear={year}&sequenceNumber={seq}"
               f"&documentNumber={docnum}&offset={offset}")
        rows = parse_journal_rows(fetch(url))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 10:
            break
        offset += 10
        time.sleep(JOURNAL_DELAY)
    return out


def discover_journal_key(case):
    """Finn journalsakens (år, sekvensnr, et gyldig dokumentnr) via tittelsøk."""
    my_titles = {norm_title(d["title"]) for d in case["documents"]}
    my_titles.discard("")
    if not my_titles:
        return None
    # Søk på de mest særpregede dokumenttitlene (nyeste først), deretter sakstittel
    queries = []
    for d in sorted(case["documents"], key=lambda x: doc_seq(x["title"]), reverse=True):
        t = re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', d["title"]).strip()
        if len(t) >= 12:
            queries.append(t)
    queries.append(case["title"])
    tried = 0
    for q in queries:
        if tried >= 3:
            break
        tried += 1
        url = f"{PJ_BASE}/Journal/SearchSimple?searchstring={urllib.parse.quote(q[:120])}"
        rows = parse_journal_rows(fetch(url))
        time.sleep(JOURNAL_DELAY)
        for r in rows:
            if r["ntitle"] in my_titles:
                return (r["year"], r["seq"], r["docnum"])
    return None


def enrich_case_dates(case, jinfo=None):
    """Sett dato på dokumentene via offentlig journal. Returnerer True ved treff."""
    if jinfo is None:
        jinfo = discover_journal_key(case)
    if jinfo is None:
        case["journalKey"] = None
        return False
    year, seq, docnum = jinfo
    jdocs = fetch_journal_case(year, seq, docnum)
    if not jdocs:
        case["journalKey"] = None
        return False
    # tittel -> datoer i journal-dokumentrekkefølge
    tmap = {}
    for r in sorted(jdocs, key=lambda x: x["docnum"]):
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
    case["journalKey"] = f"{year}-{seq}"
    case["journalDoc"] = docnum
    return hits > 0


def journal_change_feed(days):
    """Journalførte plan/bygg-dokumenter siste N dager (endringsfeed)."""
    tod = datetime.now().strftime("%d.%m.%Y")
    fromd = (datetime.now() - timedelta(days=days)).strftime("%d.%m.%Y")
    out = []
    offset = 0
    while offset <= 3000:
        url = (f"{PJ_BASE}/Journal/SearchSimple?searchstring=&daterange=custom"
               f"&fromdate={fromd}&todate={tod}&selecteddepartments={PJ_DEPTS}&offset={offset}")
        rows = parse_journal_rows(fetch(url))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 10:
            break
        offset += 10
        time.sleep(JOURNAL_DELAY)
    return out


# ---------------------------------------------------------------------------
# Status og oppsummering
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


def classify_vedtaksbrev(title):
    t = title.lower()
    if re.search(r'avslag|avslås', t):
        return "Avslag"
    if re.search(r'stoppordre', t):
        return "Stoppordre gitt"
    if re.search(r'avvis', t):
        return "Avvist"
    if re.search(r'ferdigattest', t):
        return "Avsluttet – ferdigattest gitt"
    if re.search(r'igangsetting', t):
        return "Igangsettingstillatelse gitt"
    if re.search(r'rammetillatelse', t):
        return "Rammetillatelse gitt"
    if re.search(r'godkjent|tillatelse|dispensasjon|innvilg', t):
        return "Tillatelse gitt"
    return "Vedtak fattet"


def infer_status(documents):
    docs = sorted(documents, key=lambda d: doc_seq(d["title"]), reverse=True)
    for doc in docs:
        t = doc["title"].lower()
        dtype = doc["type"].lower()
        if dtype == "vedtaksbrev":
            return classify_vedtaksbrev(doc["title"])
        outgoing = dtype in OUTGOING_TYPES
        for pattern, needs_out, status in STATUS_RULES:
            if re.search(pattern, t) and (not needs_out or outgoing):
                return status
    if documents:
        return "Under behandling"
    return "Ingen offentlige dokumenter"


def generate_summary(case):
    desc = case.get("description") or case["title"]
    n = len(case["documents"])
    parts = []
    year = "20" + case["casenr"].split("-")[1][:2] if "-" in case["casenr"] else ""
    parts.append(f"{case['type']} fra {year} som gjelder {desc.strip().rstrip('.')}." if year
                 else f"{case['type']}: {desc}.")
    if n == 0:
        parts.append("Saken har ingen offentlig tilgjengelige dokumenter i innsynsløsningen.")
    else:
        parts.append(f"Saken har {n} offentlig{'e' if n != 1 else ''} journalført{'e' if n != 1 else ''} dokument{'er' if n != 1 else ''}.")
        docs = sorted(case["documents"], key=lambda d: doc_seq(d["title"]))
        def strip_nr(t):
            return re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', t)
        first, last = docs[0], docs[-1]
        fd = f" ({fmt_no_date(first.get('date'))})" if first.get("date") else ""
        ld = f" ({fmt_no_date(last.get('date'))})" if last.get("date") else ""
        parts.append(f"Første dokument: «{strip_nr(first['title'])}»{fd}. Siste dokument: «{strip_nr(last['title'])}»{ld}.")
    parts.append(f"Vurdert status: {case['status']}.")
    return " ".join(parts)


def fmt_no_date(iso):
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


# ---------------------------------------------------------------------------
# Geokoding og adressevisning
# ---------------------------------------------------------------------------

def geocode(case, by_addr, by_gnrbnr, street_centroids):
    candidates = []
    for a in case.get("detailAddresses", []):
        m = re.match(r'(?:\d+/\d+(?:/\d+)*\s+)?(.+?),\s*\d{4}', a)
        if m:
            candidates.append(m.group(1).strip().lower())
    if case.get("listAddress"):
        candidates.append(case["listAddress"].strip().lower())
    tm = re.match(r'^(.*?)\s+\d{1,3}\s*/', case["title"])
    if tm:
        candidates.append(tm.group(1).strip().lower())

    candidates = [re.sub(r'^[\s\-–,]+', '', c) for c in candidates]
    for c in candidates:
        if c in by_addr:
            return by_addr[c], "adresse"
    gb = extract_gnrbnr(case["title"]) or next(
        (extract_gnrbnr(a) for a in case.get("detailAddresses", []) if extract_gnrbnr(a)), None)
    if gb:
        key = f"{gb[0]}/{gb[1]}"
        if key in by_gnrbnr:
            return by_gnrbnr[key], "gnr/bnr"
    for c in candidates:
        street = re.sub(r'\s+\d+[a-zæøå]?$', '', c).strip()
        if street in street_centroids:
            return street_centroids[street], "gate"
    return None, None


def display_address(case, street_names_lower, gnrbnr_addr):
    """Adresse til visning – gnr/bnr oversettes til adresse der det er mulig."""
    if case.get("listAddress"):
        return case["listAddress"]
    # fra detaljadresse: «2/122 Skibbuveien 2B, 4625 FLEKKERØY, Norge»
    for a in case.get("detailAddresses", []):
        m = re.match(r'(?:\d+/\d+(?:/\d+)*\s+)?(.+?),', a)
        if m:
            cand = m.group(1).strip()
            if cand.lower() != "ingen adresse" and any(
                    cand.lower().startswith(s) for s in street_names_lower):
                return cand
    # fra tittel: «Skibbuveien 2B 2/122/0/0, ...»
    tm = re.match(r'^[\s\-–]*(.+?)\s+\d{1,3}\s*/', case["title"])
    if tm:
        cand = tm.group(1).strip()
        if any(cand.lower().startswith(s) for s in street_names_lower):
            return cand
    # gnr/bnr -> adresse fra matrikkelen
    gb = extract_gnrbnr(case["title"])
    if gb:
        key = f"{gb[0]}/{gb[1]}"
        if key in gnrbnr_addr:
            return gnrbnr_addr[key]
    head = re.sub(r'^[\s\-–]*', '', case.get("addressHead") or case["title"].split(",")[0])
    return head.strip() or case["casenr"]


def split_title(title):
    if "," in title:
        head, desc = title.split(",", 1)
        return head.strip(), desc.strip()
    return title.strip(), ""


# ---------------------------------------------------------------------------
# Innsamling
# ---------------------------------------------------------------------------

def sweep_streets(street_names, street_names_lower, gnr_set):
    found = {}
    log(f"Gatesveip: søker OpenGov for {len(street_names)} gatenavn ...")
    for i, street in enumerate(street_names, 1):
        html = fetch(f"{SITE}?q={urllib.parse.quote(street)}")
        if html:
            for item in parse_case_list(html):
                if item["type"] in CASE_TYPES and is_flekkeroy_candidate(item, street_names_lower, gnr_set):
                    found[item["id"]] = item
        if i % 20 == 0:
            log(f"  {i}/{len(street_names)} gater, {len(found)} kandidater")
        time.sleep(REQUEST_DELAY)
    return found


def fetch_main_lists(street_names_lower, gnr_set):
    found = {}
    for tname, tid in CASE_TYPES.items():
        html = fetch(f"{SITE}?casetypeid={tid}")
        if html:
            for item in parse_case_list(html):
                if item["type"] == tname and is_flekkeroy_candidate(item, street_names_lower, gnr_set):
                    found.setdefault(item["id"], item)
        time.sleep(REQUEST_DELAY)
    return found


def build_case(cid, item, det, lookups, old_case=None):
    by_addr, by_gnrbnr, gnrbnr_addr, street_centroids, street_names_lower = lookups
    addr_head, desc = split_title(item["title"])
    gb = extract_gnrbnr(item["title"])
    case = {
        "id": cid,
        "url": item["url"],
        "title": item["title"],
        "casenr": item["casenr"],
        "type": item["type"],
        "listAddress": item["listAddress"],
        "description": desc,
        "addressHead": addr_head,
        "matrikkel": f"{gb[0]}/{gb[1]}" if gb else "",
        "saksbehandler": det["saksbehandler"],
        "detailAddresses": det["addresses"],
        "documents": sorted(det["documents"], key=lambda d: doc_seq(d["title"]), reverse=True),
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if old_case:
        for k in ("journalKey", "journalDoc"):
            if old_case.get(k):
                case[k] = old_case[k]
    case["status"] = infer_status(case["documents"])

    # Endringshistorikk: hva har skjedd siden forrige innhenting
    today = datetime.now().strftime("%Y-%m-%d")
    endringer = list((old_case or {}).get("endringer") or [])
    if old_case is None:
        if os.path.exists(CASES_JSON):  # bare interessant ved inkrementelle kjøringer
            endringer.append({"dato": today, "tekst": "Saken dukket opp i kartet"})
    else:
        n_new = len(case["documents"])
        n_old = len(old_case.get("documents") or [])
        if n_new > n_old:
            titles = [re.sub(r'^[A-ZÆØÅ]+-\d{2}/\d+-\d+\s*-\s*', '', d["title"])
                      for d in case["documents"][:n_new - n_old]]
            endringer.append({"dato": today,
                              "tekst": f"{n_new - n_old} nye dokument(er): " + "; ".join(titles[:3])})
        if old_case.get("status") and old_case["status"] != case["status"]:
            endringer.append({"dato": today,
                              "tekst": f"Status endret: {old_case['status']} → {case['status']}"})
    case["endringer"] = endringer[-25:]
    (case["latlon"], case["geoSource"]) = geocode(case, by_addr, by_gnrbnr, street_centroids)
    case["displayAddress"] = display_address(case, street_names_lower, gnrbnr_addr)
    return case


ICS_KEYWORDS = re.compile(r'befaring|frist|høring|politisk behandling', re.I)
ICS_DATE = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{4})\b')


def ics_escape(t):
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def generate_ics(cases):
    """Kalenderfeed: nye saker (siste 60 dager) + befaringer/frister nevnt i dokumenter."""
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    events = []
    for c in cases:
        addr = c.get("displayAddress") or c["casenr"]
        if c.get("firstDate") and c["firstDate"] >= cutoff:
            events.append({
                "uid": f"ny-{c['id']}@byggesak",
                "date": c["firstDate"].replace("-", ""),
                "summary": f"Ny {c['type'].lower()}: {addr}",
                "desc": f"{c.get('description') or c['title']} ({c['casenr']}) – {c['url']}",
            })
        for d in c.get("documents") or []:
            if not ICS_KEYWORDS.search(d["title"]):
                continue
            for dm in ICS_DATE.finditer(d["title"]):
                dd, mm, yyyy = dm.groups()
                iso = f"{yyyy}-{mm}-{dd}"
                if iso >= today:
                    events.append({
                        "uid": f"dok-{c['id']}-{yyyy}{mm}{dd}@byggesak",
                        "date": f"{yyyy}{mm}{dd}",
                        "summary": f"{addr}: {re.sub(r'^[A-ZÆØÅ]+-\\d{{2}}/\\d+-\\d+\\s*-\\s*', '', d['title'])[:70]}",
                        "desc": f"{c['casenr']} – {c['url']}",
                    })
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//byggesak-flekkeroy//NO",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Byggesaker Flekkerøy og Søm"]
    seen = set()
    for e in events:
        if e["uid"] in seen:
            continue
        seen.add(e["uid"])
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{e['date']}",
                  f"SUMMARY:{ics_escape(e['summary'])}",
                  f"DESCRIPTION:{ics_escape(e['desc'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path = os.path.join(DATA_DIR, "kalender.ics")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    log(f"Skrev {path} ({len(seen)} hendelser)")


def repair_dates():
    """Prøv datoberikelse på nytt for saker som mangler journalkobling (sekvensielt)."""
    with open(CASES_JSON, encoding="utf-8") as f:
        out = json.load(f)
    todo = [c for c in out["cases"] if c["documents"] and not c.get("journalKey")]
    log(f"Reparasjon: {len(todo)} saker mangler journaldatoer.")

    def save():
        out["cases"].sort(key=lambda c: (c.get("lastDate") or "", c["casenr"]), reverse=True)
        with open(CASES_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        with open(CASES_JS, "w", encoding="utf-8") as f:
            f.write("window.BYGGESAK_DATA = ")
            json.dump(out, f, ensure_ascii=False)
            f.write(";\n")

    for i, c in enumerate(todo, 1):
        try:
            if enrich_case_dates(c):
                c["summary"] = generate_summary(c)
        except Exception as e:  # noqa: BLE001
            log(f"  feil {c['casenr']}: {e}")
        if i % 25 == 0:
            fixed = sum(1 for x in todo[:i] if x.get("journalKey"))
            log(f"  {i}/{len(todo)} ({fixed} reparert) – lagrer")
            save()
        time.sleep(JOURNAL_DELAY)
    out["cases"].sort(key=lambda c: (c.get("lastDate") or "", c["casenr"]), reverse=True)
    with open(CASES_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(CASES_JS, "w", encoding="utf-8") as f:
        f.write("window.BYGGESAK_DATA = ")
        json.dump(out, f, ensure_ascii=False)
        f.write(";\n")
    fixed = sum(1 for c in todo if c.get("journalKey"))
    log(f"Reparerte {fixed}/{len(todo)} saker. Skrev {CASES_JSON} og {CASES_JS}")


def main():
    if "--dates-retry" in sys.argv:
        repair_dates()
        return
    force_full = "--full" in sys.argv
    force_sweep = "--sweep" in sys.argv
    os.makedirs(DATA_DIR, exist_ok=True)

    addresses = load_area_addresses()
    by_addr, by_gnrbnr, gnrbnr_addr, street_centroids, street_names, gnr_set = build_lookups(addresses)
    street_names_lower = [s.lower() for s in street_names]
    lookups = (by_addr, by_gnrbnr, gnrbnr_addr, street_centroids, street_names_lower)

    old_cases, old_meta = {}, {}
    if os.path.exists(CASES_JSON) and not force_full:
        with open(CASES_JSON, encoding="utf-8") as f:
            old = json.load(f)
        old_cases = {c["id"]: c for c in old.get("cases", [])}
        old_meta = {k: old.get(k) for k in ("updated", "lastSweep", "postnumre")}
        if old_meta.get("postnumre") != sorted(POSTNUMRE):
            force_sweep = True
            log(f"Områdelisten er endret ({old_meta.get('postnumre')} -> {sorted(POSTNUMRE)}) – tvinger gatesveip.")
        log(f"Inkrementell oppdatering ({len(old_cases)} kjente saker).")

    candidates = {}
    to_fetch = set()
    now = datetime.now(timezone.utc)

    if not old_cases:
        # Full innsamling
        candidates = sweep_streets(street_names, street_names_lower, gnr_set)
        candidates.update(fetch_main_lists(street_names_lower, gnr_set))
        to_fetch = set(candidates)
        last_sweep = now
        log(f"Full innsamling: {len(candidates)} kandidater.")
    else:
        # 1) Nye saker fra hovedlistene
        main_items = fetch_main_lists(street_names_lower, gnr_set)
        new_ids = {cid for cid in main_items if cid not in old_cases}
        candidates.update(main_items)
        log(f"Hovedlister: {len(new_ids)} nye saker.")

        # 2) Endringsfeed fra offentlig journal
        try:
            last_upd = datetime.strptime(old_meta.get("updated", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            days = min(max((now - last_upd).days + 2, 2), 30)
        except ValueError:
            days = 7
        feed = journal_change_feed(days)
        key_to_id = {c.get("journalKey"): cid for cid, c in old_cases.items() if c.get("journalKey")}
        changed_ids, unknown_streets = set(), set()
        for e in feed:
            if e["key"] in key_to_id:
                changed_ids.add(key_to_id[e["key"]])
            else:
                t = e["rawtitle"].lower().lstrip("-– ")
                for s in street_names_lower:
                    if t.startswith(s + " ") or t.startswith(s + ","):
                        unknown_streets.add(s)
                        break
        log(f"Journalfeed siste {days} døgn: {len(feed)} dokumenter, "
            f"{len(changed_ids)} kjente saker med aktivitet, "
            f"{len(unknown_streets)} Flekkerøy-gater med ukjent journalsak.")

        # 3) Gate-søk kun for gater med uidentifisert aktivitet
        for s in sorted(unknown_streets):
            html = fetch(f"{SITE}?q={urllib.parse.quote(s)}")
            if html:
                for item in parse_case_list(html):
                    if item["type"] in CASE_TYPES and is_flekkeroy_candidate(item, street_names_lower, gnr_set):
                        candidates.setdefault(item["id"], item)
                        if item["id"] not in old_cases:
                            new_ids.add(item["id"])
                        elif not old_cases[item["id"]].get("journalKey"):
                            changed_ids.add(item["id"])
            time.sleep(REQUEST_DELAY)

        # 4) Ukentlig sikkerhetsnett: full gatesveip
        last_sweep = None
        if old_meta.get("lastSweep"):
            try:
                last_sweep = datetime.strptime(old_meta["lastSweep"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if force_sweep or last_sweep is None or (now - last_sweep).days >= SWEEP_INTERVAL_DAYS:
            swept = sweep_streets(street_names, street_names_lower, gnr_set)
            for cid, item in swept.items():
                candidates.setdefault(cid, item)
                if cid not in old_cases:
                    new_ids.add(cid)
            last_sweep = now
        # rekonstruer listAddress m.m. for endrede saker uten ferskt listeelement
        for cid in changed_ids:
            if cid not in candidates and cid in old_cases:
                oc = old_cases[cid]
                candidates[cid] = {"id": cid, "url": oc["url"], "title": oc["title"],
                                   "casenr": oc["casenr"], "type": oc["type"],
                                   "listAddress": oc.get("listAddress", "")}
        to_fetch = new_ids | changed_ids

    log(f"Henter detaljer for {len(to_fetch)} saker ...")

    def fetch_one(cid):
        html = fetch(f"{SITE}/Case/Details/{cid}")
        time.sleep(REQUEST_DELAY)
        return cid, (parse_detail(html) if html else None)

    details = {}
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for i, (cid, det) in enumerate(ex.map(fetch_one, sorted(to_fetch)), 1):
                if det is not None:
                    details[cid] = det
                if i % 50 == 0:
                    log(f"  {i}/{len(to_fetch)} detaljsider")

    # Sett sammen sakene
    cases, dropped = [], 0
    rebuilt_ids = set()
    for cid, det in details.items():
        item = candidates.get(cid)
        if item is None:
            continue
        if det["addresses"]:
            postals = re.findall(r',\s*(\d{4})\s+\S', " | ".join(det["addresses"]))
            if postals and not any(p in POSTNUMRE for p in postals):
                dropped += 1
                rebuilt_ids.add(cid)  # utenfor Flekkerøy: ikke gjenbruk gammel versjon
                continue
        case = build_case(cid, item, det, lookups, old_cases.get(cid))
        cases.append(case)
        rebuilt_ids.add(cid)

    # Datoberikelse fra offentlig journal (kun nybygde saker)
    need_dates = [c for c in cases if c["documents"]]
    log(f"Datoberikelse fra offentlig journal for {len(need_dates)} saker ...")

    def enrich_one(case):
        jinfo = None
        if case.get("journalKey") and case.get("journalDoc"):
            y, s = case["journalKey"].split("-")
            jinfo = (int(y), int(s), case["journalDoc"])
        try:
            enrich_case_dates(case, jinfo)
        except Exception as e:  # noqa: BLE001
            log(f"  journal-feil {case['casenr']}: {e}")
        return case

    for i, case in enumerate(need_dates, 1):
        enrich_one(case)
        if i % 25 == 0:
            dated_n = sum(1 for c in need_dates[:i] if c.get('journalKey'))
            log(f"  {i}/{len(need_dates)} saker behandlet ({dated_n} datert)")
        time.sleep(JOURNAL_DELAY)

    for c in cases:
        c["summary"] = generate_summary(c)

    # Gjenbruk alle gamle saker som ikke ble bygget på nytt
    for cid, oc in old_cases.items():
        if cid not in rebuilt_ids:
            cases.append(oc)

    with_geo = [c for c in cases if c.get("latlon")]
    dated = [c for c in cases if c.get("lastDate")]
    log(f"{len(cases)} saker totalt ({dropped} forkastet, {len(cases) - len(with_geo)} uten koordinat, "
        f"{len(dated)} med datoer).")

    summaries = {}
    if os.path.exists(SUMMARIES_JSON):
        with open(SUMMARIES_JSON, encoding="utf-8") as f:
            summaries = json.load(f)
    for c in cases:
        ai = summaries.get("cases", {}).get(c["casenr"])
        if ai:
            c["aiSummary"] = ai
        else:
            c.pop("aiSummary", None)

    out = {
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedLocal": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "lastSweep": (last_sweep or now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "postnumre": sorted(POSTNUMRE),
        "source": SITE,
        "poiSummaries": summaries.get("pois", {}),
        "cases": sorted(cases, key=lambda c: (c.get("lastDate") or "", c["casenr"]), reverse=True),
    }
    with open(CASES_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(CASES_JS, "w", encoding="utf-8") as f:
        f.write("window.BYGGESAK_DATA = ")
        json.dump(out, f, ensure_ascii=False)
        f.write(";\n")
    log(f"Skrev {CASES_JSON} og {CASES_JS}")
    generate_ics(out["cases"])


if __name__ == "__main__":
    main()
