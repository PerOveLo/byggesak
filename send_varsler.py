# -*- coding: utf-8 -*-
"""
Sender varsler til abonnenter: e-post (Resend) og web-push (VAPID) når fulgte
adresser/områder får aktivitet, pluss ukessammendrag på fredager.
Kjøres av GitHub Actions rett etter oppdater_data.py.

GitHub Secrets:
  VARSLER_API_URL      Base-URL til Cloudflare-workeren
  VARSLER_API_SECRET   Delt hemmelighet for /all
  RESEND_API_KEY       resend.com API-nøkkel
  VARSLER_FRA          Avsender, f.eks. "Byggesaker Flekkerøy <varsel@dittdomene.no>"
  SITE_URL             URL til kartet (brukes i meldingene)
  VAPID_PRIVATE_KEY    (valgfri) privat VAPID-nøkkel for web-push
  VAPID_CLAIM_EMAIL    (valgfri) mailto:-adresse for VAPID-claims
  PREV_CASES           (settes av workflow) sti til forrige cases.json

Varslingsnivåer per adresse: "alt" | "vedtak" (kun vedtak/ulovlighet/pålegg) | "nye" (kun nye saker).
Uten secrets avslutter skriptet stille.
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta

API_URL = os.environ.get("VARSLER_API_URL", "").rstrip("/")
API_SECRET = os.environ.get("VARSLER_API_SECRET", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_ADDR = os.environ.get("VARSLER_FRA", "")
SITE_URL = os.environ.get("SITE_URL", "")
VAPID_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM = os.environ.get("VAPID_CLAIM_EMAIL", "")
PREV_PATH = os.environ.get("PREV_CASES", "/tmp/forrige_cases.json")

ROOT = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(ROOT, "data", "cases.json")

VEDTAK_MARKERS = ("vedtak", "tillatelse", "avslag", "pålegg", "stoppordre", "tvangsmulkt",
                  "overtredelsesgebyr", "ferdigattest", "dispensasjon")
import re as _re
_INTERNAL = _re.compile(r'byggesak|plan og bygg|oppmåling|eiendom|kristiansand kommune|tilsyn|'
                        r'byantikvar|statsforvalter|parkvesen|by- og stedsutvikling|innbygger|stab', _re.I)


def _doc_seq(t):
    m = _re.match(r'[A-ZÆØÅ]+-\d{2}/\d+-(\d+)', t or "")
    return int(m.group(1)) if m else 0


def case_soker(c):
    """Søkerfirma = avsender av dokumenter med type «Søknad» (ikke interne avdelinger)."""
    r = None
    fallback = None
    for d in sorted(c.get("documents") or [], key=lambda x: _doc_seq(x.get("title"))):
        f = (d.get("from") or "").strip()
        if not f or _INTERNAL.search(f):
            continue
        dtype = (d.get("type") or "").strip()
        title = (d.get("title") or "").lower()
        if dtype == "Søknad":
            r = f
        elif fallback is None and "dokument inn" in dtype.lower() and "søknad om" in title and \
                not _re.search(r'kommentar|merknad|uttalelse|tilsvar|svar på|nabo|klage', title):
            fallback = f
    return r or fallback


def addr_key(a):
    return (a or "").lower().strip()


def http_json(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(data).encode() if data is not None else None,
                                 method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def is_vedtak_change(texts):
    joined = " ".join(texts).lower()
    return any(m in joined for m in VEDTAK_MARKERS)


def collect_changes(new, prev):
    """-> {addrkey: {label, latlon, isNew, isUlovlighet, isVedtak, items[]}}"""
    changes = {}
    for cid, c in new.items():
        label = c.get("displayAddress") or c.get("casenr")
        key = addr_key(label)
        old = prev.get(cid)
        entry = None
        if old is None:
            entry = changes.setdefault(key, {"label": label, "latlon": c.get("latlon"),
                                             "isNew": False, "isUlovlighet": False, "isVedtak": False, "items": []})
            entry["isNew"] = True
            entry["items"].append(f"Ny sak: {c['casenr']} – {c.get('description') or c['title']} (status: {c.get('status', '?')})")
        else:
            n_new, n_old = len(c.get("documents") or []), len(old.get("documents") or [])
            if n_new > n_old:
                titles = [d["title"].split(" - ", 1)[-1] for d in (c.get("documents") or [])[:n_new - n_old]]
                entry = changes.setdefault(key, {"label": label, "latlon": c.get("latlon"),
                                                 "isNew": False, "isUlovlighet": False, "isVedtak": False, "items": []})
                entry["items"].append(f"{c['casenr']}: {n_new - n_old} nye dokument(er) – " + "; ".join(titles[:3]))
                if is_vedtak_change(titles):
                    entry["isVedtak"] = True
        if entry:
            if c.get("type") == "Ulovlighetssak":
                entry["isUlovlighet"] = True
            entry["soker"] = (case_soker(c) or "").lower()
            entry["sb"] = (c.get("saksbehandler") or "").lower().strip()
    return changes


def level_match(change, level):
    if level == "alt":
        return True
    if level == "nye":
        return change["isNew"]
    if level == "vedtak":
        return change["isNew"] or change["isVedtak"] or change["isUlovlighet"]
    return True


def user_hits(user, changes):
    hits = []
    seen = set()
    for a in user.get("addresses", []):
        label = a.get("label") if isinstance(a, dict) else a
        level = a.get("level", "alt") if isinstance(a, dict) else "alt"
        if label.startswith("firma:"):
            navn = label[6:].lower().strip()
            for ch in changes.values():
                if ch.get("soker") == navn and level_match(ch, level) and id(ch) not in seen:
                    hits.append(ch); seen.add(id(ch))
        elif label.startswith("sb:"):
            navn = label[3:].lower().strip()
            for ch in changes.values():
                if ch.get("sb") == navn and level_match(ch, level) and id(ch) not in seen:
                    hits.append(ch); seen.add(id(ch))
        else:
            ch = changes.get(addr_key(label))
            if ch and level_match(ch, level) and id(ch) not in seen:
                hits.append(ch)
                seen.add(id(ch))
    r = user.get("radius")
    if r and r.get("lat") is not None:
        for ch in changes.values():
            if id(ch) in seen or not ch.get("latlon"):
                continue
            if haversine_m(r["lat"], r["lon"], ch["latlon"][0], ch["latlon"][1]) <= r.get("m", 300):
                if level_match(ch, r.get("level", "alt")):
                    hits.append(ch)
                    seen.add(id(ch))
    return hits


def send_email(to, subject, body):
    http_json("https://api.resend.com/emails",
              data={"from": FROM_ADDR, "to": [to], "subject": subject, "text": body},
              headers={"Authorization": f"Bearer {RESEND_KEY}"})


def send_push(user, title, body):
    if not (VAPID_KEY and user.get("push")):
        return
    try:
        from pywebpush import webpush
    except ImportError:
        return
    for sub in user["push"]:
        try:
            webpush(subscription_info=sub,
                    data=json.dumps({"title": title, "body": body, "url": SITE_URL}),
                    vapid_private_key=VAPID_KEY,
                    vapid_claims={"sub": VAPID_CLAIM or "mailto:varsel@example.com"})
        except Exception as e:  # noqa: BLE001
            print(f"  push feilet: {e}", file=sys.stderr)


def format_body(hits, intro):
    lines = []
    for h in hits:
        lines.append(f"📍 {h['label']}")
        lines.extend("   • " + i for i in h["items"])
        lines.append("")
    return (f"Hei!\n\n{intro}\n\n" + "\n".join(lines)
            + (f"\nSe detaljer i kartet: {SITE_URL}\n" if SITE_URL else "")
            + "\n– Byggesaker Flekkerøy (automatisk varsel)\n")


def weekly_digest(new):
    """Ukessammendrag: nye saker siste 7 dager, gruppert per type."""
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    fresh = [c for c in new.values() if (c.get("firstDate") or "") >= cutoff]
    if not fresh:
        return None
    by_type = {}
    for c in sorted(fresh, key=lambda x: x.get("firstDate") or "", reverse=True):
        by_type.setdefault(c["type"], []).append(c)
    lines = [f"Denne uken kom det {len(fresh)} nye saker:"]
    for t, cs in by_type.items():
        lines.append(f"\n{t} ({len(cs)}):")
        for c in cs[:15]:
            ai = c.get("aiSummary")
            extra = f" – {ai['text'][:140]}…" if isinstance(ai, dict) and ai.get("text") else ""
            lines.append(f"  • {c.get('displayAddress') or ''}: {c.get('description') or c['title']}{extra}")
        if len(cs) > 15:
            lines.append(f"  … og {len(cs) - 15} til")
    return "\n".join(lines)


def main():
    if not (API_URL and API_SECRET and RESEND_KEY and FROM_ADDR):
        print("Varslings-secrets ikke satt – hopper over varsling.")
        return

    with open(CASES, encoding="utf-8") as f:
        new = {c["id"]: c for c in json.load(f)["cases"]}
    prev = {}
    if os.path.exists(PREV_PATH):
        with open(PREV_PATH, encoding="utf-8") as f:
            prev = {c["id"]: c for c in json.load(f)["cases"]}

    subs = http_json(f"{API_URL}/all", headers={"Authorization": f"Bearer {API_SECRET}"})
    sent = 0

    if prev:
        changes = collect_changes(new, prev)
        if changes:
            for email, user in subs.items():
                hits = user_hits(user, changes)
                if not hits:
                    continue
                subject = f"Ny aktivitet på {hits[0]['label']}" + (f" (+{len(hits) - 1} til)" if len(hits) > 1 else "")
                try:
                    send_email(email, subject, format_body(hits, "Det er ny aktivitet på adresser/områder du følger:"))
                    send_push(user, subject, f"{sum(len(h['items']) for h in hits)} endringer – trykk for å åpne kartet")
                    sent += 1
                except Exception as e:  # noqa: BLE001
                    print(f"Klarte ikke varsle {email}: {e}", file=sys.stderr)
        print(f"Aktivitetsvarsler: {sent} sendt ({len(changes)} adresser med endringer).")
    else:
        print("Ingen forrige datafil – hopper over aktivitetsvarsler.")

    # Ukessammendrag på fredager
    if datetime.now().weekday() == 4:
        digest = weekly_digest(new)
        if digest:
            wsent = 0
            for email, user in subs.items():
                if not user.get("weekly"):
                    continue
                try:
                    send_email(email, "Ukens byggesaker – Flekkerøy og Søm",
                               f"Hei!\n\n{digest}\n\n" + (f"Se kartet: {SITE_URL}\n" if SITE_URL else "")
                               + "\n– Byggesaker Flekkerøy (ukessammendrag)\n")
                    wsent += 1
                except Exception as e:  # noqa: BLE001
                    print(f"Ukessammendrag til {email} feilet: {e}", file=sys.stderr)
            print(f"Ukessammendrag: {wsent} sendt.")


if __name__ == "__main__":
    main()
