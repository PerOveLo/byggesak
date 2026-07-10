# -*- coding: utf-8 -*-
"""
Sender e-postvarsler til abonnenter når adresser de følger får nye saker
eller nye dokumenter. Kjøres av GitHub Actions rett etter oppdater_data.py.

Krever miljøvariabler (settes som GitHub Secrets):
  VARSLER_API_URL     Base-URL til varslings-workeren (worker/varsler-worker.js)
  VARSLER_API_SECRET  Delt hemmelighet for /all-endepunktet
  RESEND_API_KEY      API-nøkkel fra resend.com (gratis-tier holder)
  VARSLER_FRA         Avsenderadresse, f.eks. "Byggesaker Flekkerøy <varsel@dittdomene.no>"
  SITE_URL            (valgfri) URL til kartet, brukes i e-posten
  PREV_CASES          (valgfri) sti til forrige cases.json for sammenligning

Uten disse variablene avslutter skriptet stille med kode 0.
"""

import json
import os
import sys
import urllib.request

API_URL = os.environ.get("VARSLER_API_URL", "").rstrip("/")
API_SECRET = os.environ.get("VARSLER_API_SECRET", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_ADDR = os.environ.get("VARSLER_FRA", "")
SITE_URL = os.environ.get("SITE_URL", "")
PREV_PATH = os.environ.get("PREV_CASES", "/tmp/forrige_cases.json")

ROOT = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(ROOT, "data", "cases.json")


def addr_key(a):
    return (a or "").lower().strip()


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    if not (API_URL and API_SECRET and RESEND_KEY and FROM_ADDR):
        print("Varslings-secrets ikke satt – hopper over e-postvarsling.")
        return

    with open(CASES, encoding="utf-8") as f:
        new = {c["id"]: c for c in json.load(f)["cases"]}
    prev = {}
    if os.path.exists(PREV_PATH):
        with open(PREV_PATH, encoding="utf-8") as f:
            prev = {c["id"]: c for c in json.load(f)["cases"]}
    if not prev:
        print("Ingen forrige datafil – hopper over (første kjøring).")
        return

    # Endringer per adresse
    changes = {}  # addrkey -> {"label": str, "items": [tekstlinjer]}
    for cid, c in new.items():
        label = c.get("displayAddress") or c.get("casenr")
        key = addr_key(label)
        old = prev.get(cid)
        if old is None:
            changes.setdefault(key, {"label": label, "items": []})["items"].append(
                f"Ny sak: {c['casenr']} – {c.get('description') or c['title']} (status: {c.get('status','?')})")
        else:
            n_new, n_old = len(c.get("documents") or []), len(old.get("documents") or [])
            if n_new > n_old:
                titles = [d["title"].split(" - ", 1)[-1] for d in (c.get("documents") or [])[:n_new - n_old]]
                changes.setdefault(key, {"label": label, "items": []})["items"].append(
                    f"{c['casenr']}: {n_new - n_old} nye dokument(er) – " + "; ".join(titles[:3]))

    if not changes:
        print("Ingen endringer – ingen varsler sendes.")
        return

    subs = http_json(f"{API_URL}/all", headers={"Authorization": f"Bearer {API_SECRET}"})
    sent = 0
    for email, info in subs.items():
        hits = []
        for a in info.get("addresses", []):
            ch = changes.get(addr_key(a))
            if ch:
                hits.append(ch)
        if not hits:
            continue
        lines = []
        for h in hits:
            lines.append(f"📍 {h['label']}")
            lines.extend("   • " + i for i in h["items"])
            lines.append("")
        body = ("Hei!\n\nDet er ny aktivitet på adresser du følger på Flekkerøy:\n\n"
                + "\n".join(lines)
                + (f"\nSe detaljer i kartet: {SITE_URL}\n" if SITE_URL else "")
                + "\n– Byggesaker Flekkerøy (automatisk varsel)\n"
                + (f"\nAdministrer varslinger: {SITE_URL} → ★ Følger\n" if SITE_URL else ""))
        try:
            http_json("https://api.resend.com/emails",
                      data={"from": FROM_ADDR, "to": [email],
                            "subject": f"Ny aktivitet på {hits[0]['label']}" + (f" (+{len(hits)-1} til)" if len(hits) > 1 else ""),
                            "text": body},
                      headers={"Authorization": f"Bearer {RESEND_KEY}"})
            sent += 1
        except Exception as e:  # noqa: BLE001
            print(f"Klarte ikke sende til {email}: {e}", file=sys.stderr)
    print(f"Sendte {sent} varsel-e-post(er) for {len(changes)} adresser med endringer.")


if __name__ == "__main__":
    main()
