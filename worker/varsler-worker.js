/**
 * Cloudflare Worker for byggesakskartet: e-postabonnementer + PDF-proxy.
 *
 * Oppsett (5 min, gratis-tier):
 *   1. Cloudflare-dashboard → Workers & Pages → Create Worker → lim inn denne filen.
 *   2. Settings → Bindings → KV Namespace: opprett "VARSLER" og bind som `VARSLER`.
 *   3. Settings → Variables → Secret: `API_SECRET` = en lang tilfeldig streng.
 *      (Samme verdi legges som GitHub Secret VARSLER_API_SECRET.)
 *   4. Noter worker-URLen (https://<navn>.<konto>.workers.dev) og legg den inn i
 *      config.js (apiBase + pdfProxy) og som GitHub Secret VARSLER_API_URL.
 *
 * Endepunkter:
 *   POST /subscribe      {email, addresses: ["Skibbuveien 2B", ...]}  (erstatter listen)
 *   GET  /subscriptions?email=...                                     → {email, addresses}
 *   POST /unsubscribe    {email}                                      (sletter alt)
 *   GET  /all            Authorization: Bearer <API_SECRET>           → {email: {addresses}}
 *   GET  /pdfproxy?url=https://opengov.360online.com/...              → proxyet PDF med CORS
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });
const err = (msg, status) => json({ error: msg }, status);

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const ALLOWED_PDF_HOSTS = new Set(["opengov.360online.com"]);
const MAX_ADDRESSES = 50;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    try {
      if (url.pathname === "/subscribe" && req.method === "POST") {
        const { email, addresses } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        if (!Array.isArray(addresses) || !addresses.length) return err("Ingen adresser oppgitt", 400);
        const clean = [...new Set(addresses.map(a => String(a).trim()).filter(Boolean))].slice(0, MAX_ADDRESSES);
        await env.VARSLER.put("sub:" + e, JSON.stringify({ addresses: clean, updated: new Date().toISOString() }));
        return json({ ok: true, email: e, addresses: clean });
      }

      if (url.pathname === "/subscriptions" && req.method === "GET") {
        const e = (url.searchParams.get("email") || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const raw = await env.VARSLER.get("sub:" + e);
        return json({ email: e, addresses: raw ? JSON.parse(raw).addresses : [] });
      }

      if (url.pathname === "/unsubscribe" && req.method === "POST") {
        const { email } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        await env.VARSLER.delete("sub:" + e);
        return json({ ok: true });
      }

      if (url.pathname === "/all" && req.method === "GET") {
        const auth = req.headers.get("Authorization") || "";
        if (auth !== "Bearer " + env.API_SECRET) return err("Uautorisert", 401);
        const out = {};
        let cursor;
        do {
          const page = await env.VARSLER.list({ prefix: "sub:", cursor });
          for (const k of page.keys) {
            const raw = await env.VARSLER.get(k.name);
            if (raw) out[k.name.slice(4)] = JSON.parse(raw);
          }
          cursor = page.list_complete ? null : page.cursor;
        } while (cursor);
        return json(out);
      }

      if (url.pathname === "/pdfproxy" && req.method === "GET") {
        const target = url.searchParams.get("url") || "";
        const t = new URL(target);
        if (t.protocol !== "https:" || !ALLOWED_PDF_HOSTS.has(t.hostname)) return err("Ugyldig mål", 403);
        const upstream = await fetch(target, { cf: { cacheTtl: 3600, cacheEverything: true } });
        const headers = new Headers(CORS);
        headers.set("Content-Type", upstream.headers.get("Content-Type") || "application/pdf");
        headers.set("Cache-Control", "public, max-age=3600");
        return new Response(upstream.body, { status: upstream.status, headers });
      }

      return err("Ukjent endepunkt", 404);
    } catch (e) {
      return err("Serverfeil: " + (e && e.message), 500);
    }
  },
};
