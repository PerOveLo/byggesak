/**
 * Cloudflare Worker for byggesakskartet: innlogging (magisk lenke), profiler,
 * varslingsnivåer, radius-varsling, private notater, web-push-abonnementer,
 * ukessammendrag-påmelding og PDF-proxy.
 *
 * Oppsett (gratis-tier):
 *   1. Cloudflare-dashboard → Workers & Pages → Create Worker → lim inn denne filen.
 *   2. Bindings → KV Namespace: opprett "VARSLER" og bind som `VARSLER`.
 *   3. Variables & Secrets:
 *        API_SECRET      (secret)  lang tilfeldig streng – samme som GitHub Secret VARSLER_API_SECRET
 *        RESEND_API_KEY  (secret)  fra resend.com – for innloggings-e-poster
 *        FROM_ADDR       (var)     f.eks. "Byggesaker Flekkerøy <varsel@dittdomene.no>"
 *        SITE_URL        (var)     f.eks. "https://perovelo.github.io/byggesak/"
 *   4. Legg worker-URLen i config.js (apiBase + pdfProxy) og som GitHub Secret VARSLER_API_URL.
 *
 * Endepunkter (alle med åpen CORS):
 *   POST /login         {email}                 → sender magisk innloggingslenke på e-post
 *   GET  /verify?token=...                      → {session, email} (sesjon varer 90 dager)
 *   GET  /me            Bearer <session>        → profil {email, addresses, radius, weekly}
 *   PUT  /me            Bearer <session> {addresses,radius,weekly} → lagrer profil
 *   GET  /notes         Bearer <session>        → {casenr: {text, updated}}
 *   PUT  /notes         Bearer <session> {notes} → lagrer notater
 *   POST /push          Bearer <session> {subscription} → lagrer web-push-abonnement
 *   POST /subscribe     {email, addresses}      → v1-kompatibel (uinnlogget) registrering
 *   GET  /subscriptions?email=...               → v1-kompatibel adresseliste
 *   POST /unsubscribe   {email}                 → sletter profil
 *   GET  /all           Bearer <API_SECRET>     → alle profiler (for GitHub Actions)
 *   GET  /pdfproxy?url=https://opengov...       → proxyet PDF med CORS
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });
const err = (msg, status) => json({ error: msg }, status);

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const ALLOWED_PDF_HOSTS = new Set(["opengov.360online.com"]);
const MAX_ADDRESSES = 100;
const LEVELS = new Set(["alt", "vedtak", "nye"]);

const token = () => crypto.randomUUID().replaceAll("-", "") + crypto.randomUUID().replaceAll("-", "");

async function getUser(env, email) {
  const raw = await env.VARSLER.get("user:" + email);
  return raw ? JSON.parse(raw) : { addresses: [], radius: null, weekly: false, push: [] };
}
async function putUser(env, email, user) {
  user.updated = new Date().toISOString();
  await env.VARSLER.put("user:" + email, JSON.stringify(user));
}
async function requireSession(req, env) {
  const auth = req.headers.get("Authorization") || "";
  if (!auth.startsWith("Bearer ")) return null;
  return env.VARSLER.get("session:" + auth.slice(7));
}
function cleanAddresses(addresses) {
  const out = [];
  const seen = new Set();
  for (const a of addresses || []) {
    const label = String(a && a.label != null ? a.label : a).trim();
    const level = LEVELS.has(a && a.level) ? a.level : "alt";
    if (!label || seen.has(label.toLowerCase())) continue;
    seen.add(label.toLowerCase());
    out.push({ label, level });
    if (out.length >= MAX_ADDRESSES) break;
  }
  return out;
}
function cleanRadius(r) {
  if (!r || typeof r.lat !== "number" || typeof r.lon !== "number") return null;
  const m = Math.min(Math.max(parseInt(r.m, 10) || 300, 50), 5000);
  return { lat: r.lat, lon: r.lon, m, level: LEVELS.has(r.level) ? r.level : "alt" };
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    try {
      // ---------- Innlogging ----------
      if (url.pathname === "/login" && req.method === "POST") {
        const { email } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const t = token();
        await env.VARSLER.put("login:" + t, e, { expirationTtl: 900 });
        const link = (env.SITE_URL || "").replace(/\/$/, "") + "/?login=" + t;
        const resp = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + env.RESEND_API_KEY },
          body: JSON.stringify({
            from: env.FROM_ADDR, to: [e],
            subject: "Innlogging – Byggesaker Flekkerøy",
            text: `Hei!\n\nKlikk lenken for å logge inn (gyldig i 15 minutter):\n${link}\n\nHvis du ikke ba om denne, kan du se bort fra e-posten.`,
          }),
        });
        if (!resp.ok) return err("Klarte ikke sende e-post: " + (await resp.text()), 502);
        return json({ ok: true });
      }

      if (url.pathname === "/verify" && req.method === "GET") {
        const t = url.searchParams.get("token") || "";
        const email = await env.VARSLER.get("login:" + t);
        if (!email) return err("Lenken er utløpt eller ugyldig", 401);
        await env.VARSLER.delete("login:" + t);
        const s = token();
        await env.VARSLER.put("session:" + s, email, { expirationTtl: 90 * 86400 });
        return json({ session: s, email });
      }

      // ---------- Profil (innlogget) ----------
      if (url.pathname === "/me") {
        const email = await requireSession(req, env);
        if (!email) return err("Ikke innlogget", 401);
        if (req.method === "GET") {
          const u = await getUser(env, email);
          return json({ email, addresses: u.addresses, radius: u.radius || null, weekly: !!u.weekly });
        }
        if (req.method === "PUT") {
          const body = await req.json();
          const u = await getUser(env, email);
          if ("addresses" in body) u.addresses = cleanAddresses(body.addresses);
          if ("radius" in body) u.radius = cleanRadius(body.radius);
          if ("weekly" in body) u.weekly = !!body.weekly;
          await putUser(env, email, u);
          return json({ ok: true });
        }
      }

      if (url.pathname === "/notes") {
        const email = await requireSession(req, env);
        if (!email) return err("Ikke innlogget", 401);
        if (req.method === "GET") {
          const raw = await env.VARSLER.get("notes:" + email);
          return json(raw ? JSON.parse(raw) : {});
        }
        if (req.method === "PUT") {
          const { notes } = await req.json();
          if (typeof notes !== "object" || notes === null) return err("Ugyldige notater", 400);
          const clean = {};
          for (const [k, v] of Object.entries(notes).slice(0, 500)) {
            if (v && v.text) clean[String(k).slice(0, 40)] = { text: String(v.text).slice(0, 4000), updated: v.updated || new Date().toISOString() };
          }
          await env.VARSLER.put("notes:" + email, JSON.stringify(clean));
          return json({ ok: true });
        }
      }

      if (url.pathname === "/push" && req.method === "POST") {
        const email = await requireSession(req, env);
        if (!email) return err("Ikke innlogget", 401);
        const { subscription } = await req.json();
        if (!subscription || !subscription.endpoint) return err("Ugyldig abonnement", 400);
        const u = await getUser(env, email);
        u.push = (u.push || []).filter(p => p.endpoint !== subscription.endpoint);
        u.push.push(subscription);
        u.push = u.push.slice(-5);
        await putUser(env, email, u);
        return json({ ok: true });
      }

      // ---------- v1-kompatibelt (uinnlogget) ----------
      if (url.pathname === "/subscribe" && req.method === "POST") {
        const { email, addresses } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const clean = cleanAddresses(addresses);
        if (!clean.length) return err("Ingen adresser oppgitt", 400);
        const u = await getUser(env, e);
        u.addresses = clean;
        await putUser(env, e, u);
        return json({ ok: true, email: e, addresses: clean.map(a => a.label) });
      }

      if (url.pathname === "/subscriptions" && req.method === "GET") {
        const e = (url.searchParams.get("email") || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const u = await getUser(env, e);
        return json({ email: e, addresses: (u.addresses || []).map(a => a.label) });
      }

      if (url.pathname === "/unsubscribe" && req.method === "POST") {
        const { email } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        await env.VARSLER.delete("user:" + e);
        return json({ ok: true });
      }

      // ---------- For GitHub Actions ----------
      if (url.pathname === "/all" && req.method === "GET") {
        const auth = req.headers.get("Authorization") || "";
        if (auth !== "Bearer " + env.API_SECRET) return err("Uautorisert", 401);
        const out = {};
        let cursor;
        do {
          const page = await env.VARSLER.list({ prefix: "user:", cursor });
          for (const k of page.keys) {
            const raw = await env.VARSLER.get(k.name);
            if (raw) out[k.name.slice(5)] = JSON.parse(raw);
          }
          cursor = page.list_complete ? null : page.cursor;
        } while (cursor);
        return json(out);
      }

      // ---------- PDF-proxy ----------
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
