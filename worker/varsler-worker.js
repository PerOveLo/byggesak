/**
 * Byggesaker Kristiansand – API-worker v3 (Cloudflare Workers + D1).
 * Innlogging (magisk lenke), profiler/følgelister/notater/push, adminside med
 * append-only revisjonslogg, GDPR-eksport/-sletting og PDF-proxy.
 *
 * Oppsett: se wrangler.toml. Skjema: schema.sql (speiler fremtidig Postgres).
 *
 * Endepunkter (åpen CORS):
 *   POST /login {email}            magisk innloggingslenke på e-post
 *   GET  /verify?token=            -> {session, email, rolle} (90 dager)
 *   GET/PUT /me                    profil {email, addresses[{label,level}], radius, weekly}
 *                                  (labels med prefiks "firma:"/"sb:" = fulgte entiteter)
 *   GET/PUT /notes                 private notater {saksnr: {text, updated}}
 *   POST /push {subscription}      web-push-abonnement
 *   GET  /me/export                GDPR: all data om innlogget bruker (JSON)
 *   POST /me/delete                GDPR: slett konto + alle data
 *   POST /subscribe, GET /subscriptions, POST /unsubscribe   (v1-kompatibelt)
 *   GET  /all                      Bearer API_SECRET – alle profiler (for varselutsending)
 *   GET  /pdfproxy?url=            proxyet PDF med CORS
 *   Admin (krever rolle=admin):
 *   GET  /admin/stats              nøkkeltall
 *   GET  /admin/users?q=&limit=    brukerliste
 *   PUT  /admin/user {id,tier?,rolle?}
 *   POST /admin/user/delete {id}
 *   GET  /admin/audit?bruker=&handling=&fra=&til=&limit=
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });
const err = (msg, status) => json({ error: msg }, status);
const now = () => new Date().toISOString();
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const ALLOWED_PDF_HOSTS = new Set(["opengov.360online.com"]);
const LEVELS = new Set(["alt", "vedtak", "nye"]);
const token = () => crypto.randomUUID().replaceAll("-", "") + crypto.randomUUID().replaceAll("-", "");
const ADMIN_NAVN = {
  "per@vizbo.no": "Per-Ove Løvsland",
  "sjur@vizbo.no": "Sjur Magnus Ringøen",
  "david@fbc-ark.no": "David Naglestad",
};

async function audit(env, req, bruker, rolle, handling, objekt, detaljer) {
  try {
    await env.DB.prepare(
      "INSERT INTO audit_log (ts, bruker, rolle, handling, objekt, detaljer, ip, user_agent) VALUES (?,?,?,?,?,?,?,?)")
      .bind(now(), bruker || null, rolle || null, handling, objekt || null,
            detaljer ? String(detaljer).slice(0, 500) : null,
            req.headers.get("CF-Connecting-IP") || "", (req.headers.get("User-Agent") || "").slice(0, 200))
      .run();
  } catch (e) { console.log("audit-feil:", e.message); }
}

async function getSession(req, env) {
  const auth = req.headers.get("Authorization") || "";
  if (!auth.startsWith("Bearer ")) return null;
  const row = await env.DB.prepare(
    `SELECT s.token, b.id, b.epost, b.rolle, b.tier FROM sesjoner s JOIN brukere b ON b.id = s.bruker_id
     WHERE s.token = ? AND s.utloper > ?`).bind(auth.slice(7), now()).first();
  return row || null;
}

async function hashPassword(pw, salt) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(salt), iterations: 100000, hash: "SHA-256" }, key, 256);
  return [...new Uint8Array(bits)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function makeSession(env, user) {
  const s = token();
  await env.DB.prepare("INSERT INTO sesjoner (token, bruker_id, opprettet, utloper) VALUES (?,?,?,?)")
    .bind(s, user.id, now(), new Date(Date.now() + 90 * 86400 * 1000).toISOString()).run();
  return s;
}

async function upsertUser(env, epost) {
  const admins = (env.ADMIN_EPOST || "").toLowerCase().split(",").map(s => s.trim()).filter(Boolean);
  const rolle = admins.includes(epost) ? "admin" : "bruker";
  await env.DB.prepare(
    `INSERT INTO brukere (epost, navn, rolle, opprettet, sist_innlogget) VALUES (?,?,?,?,?)
     ON CONFLICT(epost) DO UPDATE SET sist_innlogget = excluded.sist_innlogget,
       navn = COALESCE(excluded.navn, brukere.navn),
       rolle = CASE WHEN excluded.rolle = 'admin' THEN 'admin' ELSE brukere.rolle END`)
    .bind(epost, ADMIN_NAVN[epost] || null, rolle, now(), now()).run();
  return env.DB.prepare("SELECT * FROM brukere WHERE epost = ?").bind(epost).first();
}

async function readProfile(env, brukerId) {
  const rows = (await env.DB.prepare("SELECT * FROM folger WHERE bruker_id = ?").bind(brukerId).all()).results || [];
  const addresses = [];
  let radius = null;
  for (const r of rows) {
    if (r.slag === "omraade") radius = { lat: r.lat, lon: r.lon, m: r.radius_m, level: r.nivaa };
    else if (r.slag === "adresse") addresses.push({ label: r.verdi, level: r.nivaa });
    else if (r.slag === "firma" || r.slag === "sb") addresses.push({ label: r.slag + ":" + r.verdi, level: r.nivaa });
  }
  const inn = await env.DB.prepare("SELECT ukesammendrag FROM innstillinger WHERE bruker_id = ?").bind(brukerId).first();
  return { addresses, radius, weekly: !!(inn && inn.ukesammendrag) };
}

async function writeProfile(env, brukerId, body) {
  if ("addresses" in body) {
    await env.DB.prepare("DELETE FROM folger WHERE bruker_id = ? AND slag IN ('adresse','firma','sb')").bind(brukerId).run();
    const seen = new Set();
    for (const a of (body.addresses || []).slice(0, 200)) {
      let label = String(a && a.label != null ? a.label : a).trim();
      const nivaa = LEVELS.has(a && a.level) ? a.level : "alt";
      let slag = "adresse";
      const m = /^(firma|sb):(.+)$/.exec(label);
      if (m) { slag = m[1]; label = m[2].trim(); }
      const key = slag + "|" + label.toLowerCase();
      if (!label || seen.has(key)) continue;
      seen.add(key);
      await env.DB.prepare(
        "INSERT OR IGNORE INTO folger (bruker_id, slag, verdi, nivaa, opprettet) VALUES (?,?,?,?,?)")
        .bind(brukerId, slag, label, nivaa, now()).run();
    }
  }
  if ("radius" in body) {
    await env.DB.prepare("DELETE FROM folger WHERE bruker_id = ? AND slag = 'omraade'").bind(brukerId).run();
    const r = body.radius;
    if (r && typeof r.lat === "number" && typeof r.lon === "number") {
      const m = Math.min(Math.max(parseInt(r.m, 10) || 300, 50), 5000);
      await env.DB.prepare(
        "INSERT INTO folger (bruker_id, slag, verdi, nivaa, lat, lon, radius_m, opprettet) VALUES (?,?,?,?,?,?,?,?)")
        .bind(brukerId, "omraade", "", LEVELS.has(r.level) ? r.level : "alt", r.lat, r.lon, m, now()).run();
    }
  }
  if ("weekly" in body) {
    await env.DB.prepare(
      `INSERT INTO innstillinger (bruker_id, ukesammendrag) VALUES (?,?)
       ON CONFLICT(bruker_id) DO UPDATE SET ukesammendrag = excluded.ukesammendrag`)
      .bind(brukerId, body.weekly ? 1 : 0).run();
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    try {
      // ---------- Innlogging ----------
      if (p === "/login" && req.method === "POST") {
        const { email } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const t = token();
        await env.DB.prepare("INSERT INTO login_tokens (token, epost, utloper) VALUES (?,?,?)")
          .bind(t, e, new Date(Date.now() + 15 * 60 * 1000).toISOString()).run();
        const link = (env.SITE_URL || "").replace(/\/$/, "") + "/?login=" + t;
        const resp = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + env.RESEND_API_KEY },
          body: JSON.stringify({ from: env.FROM_ADDR, to: [e], subject: "Innlogging – Byggesaker Kristiansand",
            text: `Hei!\n\nKlikk lenken for å logge inn (gyldig i 15 minutter):\n${link}\n\nHvis du ikke ba om denne, se bort fra e-posten.` }),
        });
        if (!resp.ok) return err("Klarte ikke sende e-post: " + (await resp.text()), 502);
        await audit(env, req, e, null, "login_lenke_sendt", null, null);
        return json({ ok: true });
      }

      // Passordinnlogging: superadmin bruker delt adminpassord (env.ADMIN_PASSORD);
      // vanlige brukere registreres automatisk med eget passord første gang.
      if (p === "/login-passord" && req.method === "POST") {
        const { email, password } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        if (!password) return err("Oppgi passord", 400);
        const admins = (env.ADMIN_EPOST || "").toLowerCase().split(",").map(s => s.trim()).filter(Boolean);
        let user;
        if (admins.includes(e)) {
          // Superadmin: personlig passord (om satt) ELLER det delte adminpassordet
          const eks = await env.DB.prepare("SELECT * FROM brukere WHERE epost = ?").bind(e).first();
          let ok = false;
          if (eks && eks.passord_hash) {
            const [salt, hash] = eks.passord_hash.split("$");
            ok = (await hashPassword(password, salt)) === hash;
          }
          if (!ok && env.ADMIN_PASSORD && password === env.ADMIN_PASSORD) ok = true;
          if (!ok) {
            await audit(env, req, e, null, "innlogging_feilet", null, "feil adminpassord");
            return err("Feil passord", 401);
          }
          user = await upsertUser(env, e);
        } else {
          user = await env.DB.prepare("SELECT * FROM brukere WHERE epost = ?").bind(e).first();
          if (user && user.passord_hash) {
            const [salt, hash] = user.passord_hash.split("$");
            if ((await hashPassword(password, salt)) !== hash) {
              await audit(env, req, e, null, "innlogging_feilet", null, null);
              return err("Feil passord", 401);
            }
            await env.DB.prepare("UPDATE brukere SET sist_innlogget = ? WHERE id = ?").bind(now(), user.id).run();
          } else {
            // ny bruker (eller eksisterende uten passord): sett passordet nå
            if (password.length < 8) return err("Passord må ha minst 8 tegn", 400);
            const salt = crypto.randomUUID();
            const ph = salt + "$" + (await hashPassword(password, salt));
            user = await upsertUser(env, e);
            await env.DB.prepare("UPDATE brukere SET passord_hash = ? WHERE id = ?").bind(ph, user.id).run();
            await audit(env, req, e, user.rolle, "bruker_registrert", null, null);
          }
        }
        const s = await makeSession(env, user);
        await audit(env, req, e, user.rolle, "innlogging", null, "passord");
        return json({ session: s, email: user.epost, navn: user.navn || ADMIN_NAVN[user.epost] || null, rolle: user.rolle, tier: user.tier });
      }

      // Brukshendelser (anonymt OK) – gir adminstatistikk over hva folk ser på
      if (p === "/hendelse" && req.method === "POST") {
        const { type, objekt } = await req.json();
        if (!["sak", "poi", "sok", "liste"].includes(type)) return err("Ugyldig type", 400);
        const sess = await getSession(req, env);
        await env.DB.prepare("INSERT INTO hendelser (ts, bruker, type, objekt) VALUES (?,?,?,?)")
          .bind(now(), sess ? sess.epost : null, type, String(objekt || "").slice(0, 120)).run();
        return json({ ok: true });
      }

      if (p === "/verify" && req.method === "GET") {
        const t = url.searchParams.get("token") || "";
        const row = await env.DB.prepare("SELECT * FROM login_tokens WHERE token = ? AND utloper > ?")
          .bind(t, now()).first();
        if (!row) return err("Lenken er utløpt eller ugyldig", 401);
        await env.DB.prepare("DELETE FROM login_tokens WHERE token = ?").bind(t).run();
        const user = await upsertUser(env, row.epost);
        const s = await makeSession(env, user);
        await audit(env, req, user.epost, user.rolle, "innlogging", null, "magisk lenke");
        return json({ session: s, email: user.epost, navn: user.navn || ADMIN_NAVN[user.epost] || null, rolle: user.rolle, tier: user.tier });
      }

      // ---------- Profil ----------
      if (p === "/me") {
        const sess = await getSession(req, env);
        if (!sess) return err("Ikke innlogget", 401);
        if (req.method === "GET") {
          const prof = await readProfile(env, sess.id);
          const u = await env.DB.prepare("SELECT navn FROM brukere WHERE id = ?").bind(sess.id).first();
          return json({ email: sess.epost, navn: (u && u.navn) || null, rolle: sess.rolle, tier: sess.tier, ...prof });
        }
        if (req.method === "PUT") {
          await writeProfile(env, sess.id, await req.json());
          await audit(env, req, sess.epost, sess.rolle, "profil_oppdatert", null, null);
          return json({ ok: true });
        }
      }

      if (p === "/notes") {
        const sess = await getSession(req, env);
        if (!sess) return err("Ikke innlogget", 401);
        if (req.method === "GET") {
          const rows = (await env.DB.prepare("SELECT saksnr, tekst, oppdatert FROM notater WHERE bruker_id = ?")
            .bind(sess.id).all()).results || [];
          const out = {};
          for (const r of rows) out[r.saksnr] = { text: r.tekst, updated: r.oppdatert };
          return json(out);
        }
        if (req.method === "PUT") {
          const { notes } = await req.json();
          if (typeof notes !== "object" || notes === null) return err("Ugyldige notater", 400);
          await env.DB.prepare("DELETE FROM notater WHERE bruker_id = ?").bind(sess.id).run();
          for (const [k, v] of Object.entries(notes).slice(0, 500)) {
            if (v && v.text) {
              await env.DB.prepare("INSERT INTO notater (bruker_id, saksnr, tekst, oppdatert) VALUES (?,?,?,?)")
                .bind(sess.id, String(k).slice(0, 40), String(v.text).slice(0, 4000), v.updated || now()).run();
            }
          }
          return json({ ok: true });
        }
      }

      if (p === "/push" && req.method === "POST") {
        const sess = await getSession(req, env);
        if (!sess) return err("Ikke innlogget", 401);
        const { subscription } = await req.json();
        if (!subscription || !subscription.endpoint) return err("Ugyldig abonnement", 400);
        await env.DB.prepare(
          `INSERT INTO push_abonnement (endpoint, bruker_id, data, opprettet) VALUES (?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET data = excluded.data`)
          .bind(subscription.endpoint, sess.id, JSON.stringify(subscription), now()).run();
        await audit(env, req, sess.epost, sess.rolle, "push_aktivert", null, null);
        return json({ ok: true });
      }

      // ---------- GDPR ----------
      if (p === "/me/export" && req.method === "GET") {
        const sess = await getSession(req, env);
        if (!sess) return err("Ikke innlogget", 401);
        const [prof, notes, push, logg] = await Promise.all([
          readProfile(env, sess.id),
          env.DB.prepare("SELECT * FROM notater WHERE bruker_id = ?").bind(sess.id).all(),
          env.DB.prepare("SELECT endpoint, opprettet FROM push_abonnement WHERE bruker_id = ?").bind(sess.id).all(),
          env.DB.prepare("SELECT ts, handling, objekt, ip FROM audit_log WHERE bruker = ? ORDER BY ts DESC LIMIT 1000")
            .bind(sess.epost).all(),
        ]);
        await audit(env, req, sess.epost, sess.rolle, "gdpr_eksport", null, null);
        return json({ epost: sess.epost, tier: sess.tier, profil: prof,
                      notater: notes.results, push: push.results, aktivitetslogg: logg.results });
      }

      if (p === "/me/delete" && req.method === "POST") {
        const sess = await getSession(req, env);
        if (!sess) return err("Ikke innlogget", 401);
        for (const t of ["folger", "notater", "push_abonnement", "innstillinger", "sesjoner"]) {
          await env.DB.prepare(`DELETE FROM ${t} WHERE bruker_id = ?`).bind(sess.id).run();
        }
        await env.DB.prepare("DELETE FROM brukere WHERE id = ?").bind(sess.id).run();
        await audit(env, req, sess.epost, sess.rolle, "gdpr_sletting", "bruker:" + sess.id, null);
        return json({ ok: true });
      }

      // ---------- Admin ----------
      if (p.startsWith("/admin/")) {
        const sess = await getSession(req, env);
        if (!sess || sess.rolle !== "admin") return err("Krever admin", 403);

        if (p === "/admin/stats" && req.method === "GET") {
          const q = async sql => (await env.DB.prepare(sql).first())?.n ?? 0;
          const stats = {
            brukere: await q("SELECT COUNT(*) n FROM brukere"),
            innloggetSiste7d: await q("SELECT COUNT(*) n FROM brukere WHERE sist_innlogget > datetime('now','-7 days')"),
            folger: await q("SELECT COUNT(*) n FROM folger"),
            notater: await q("SELECT COUNT(*) n FROM notater"),
            push: await q("SELECT COUNT(*) n FROM push_abonnement"),
            auditRader: await q("SELECT COUNT(*) n FROM audit_log"),
            perTier: (await env.DB.prepare("SELECT tier, COUNT(*) n FROM brukere GROUP BY tier").all()).results,
            hendelser7d: await q("SELECT COUNT(*) n FROM hendelser WHERE ts > datetime('now','-7 days')"),
            toppSaker: (await env.DB.prepare(
              `SELECT objekt, COUNT(*) n FROM hendelser WHERE type = 'sak' AND ts > datetime('now','-30 days')
               GROUP BY objekt ORDER BY n DESC LIMIT 12`).all()).results,
            toppAdresser: (await env.DB.prepare(
              `SELECT objekt, COUNT(*) n FROM hendelser WHERE type = 'poi' AND ts > datetime('now','-30 days')
               GROUP BY objekt ORDER BY n DESC LIMIT 12`).all()).results,
            toppFulgte: (await env.DB.prepare(
              `SELECT slag, verdi, COUNT(*) n FROM folger WHERE slag != 'omraade'
               GROUP BY slag, verdi ORDER BY n DESC LIMIT 15`).all()).results,
          };
          await audit(env, req, sess.epost, "admin", "admin_stats", null, null);
          return json(stats);
        }

        if (p === "/admin/users" && req.method === "GET") {
          const q = (url.searchParams.get("q") || "").toLowerCase();
          const limit = Math.min(parseInt(url.searchParams.get("limit"), 10) || 50, 200);
          const rows = (await env.DB.prepare(
            `SELECT b.id, b.epost, b.navn, b.rolle, b.tier, b.opprettet, b.sist_innlogget,
                    (SELECT COUNT(*) FROM folger f WHERE f.bruker_id = b.id) folger,
                    (SELECT COUNT(*) FROM notater n WHERE n.bruker_id = b.id) notater
             FROM brukere b WHERE b.epost LIKE ? ORDER BY b.sist_innlogget DESC LIMIT ?`)
            .bind(`%${q}%`, limit).all()).results;
          await audit(env, req, sess.epost, "admin", "admin_brukersok", null, q || "(alle)");
          return json(rows);
        }

        if (p === "/admin/user" && req.method === "PUT") {
          const { id, tier, rolle } = await req.json();
          if (tier) await env.DB.prepare("UPDATE brukere SET tier = ? WHERE id = ?").bind(tier, id).run();
          if (rolle) await env.DB.prepare("UPDATE brukere SET rolle = ? WHERE id = ?").bind(rolle, id).run();
          await audit(env, req, sess.epost, "admin", "admin_bruker_endret", "bruker:" + id,
                      JSON.stringify({ tier, rolle }));
          return json({ ok: true });
        }

        if (p === "/admin/user/delete" && req.method === "POST") {
          const { id } = await req.json();
          const u = await env.DB.prepare("SELECT epost FROM brukere WHERE id = ?").bind(id).first();
          if (!u) return err("Finnes ikke", 404);
          for (const t of ["folger", "notater", "push_abonnement", "innstillinger", "sesjoner"]) {
            await env.DB.prepare(`DELETE FROM ${t} WHERE bruker_id = ?`).bind(id).run();
          }
          await env.DB.prepare("DELETE FROM brukere WHERE id = ?").bind(id).run();
          await audit(env, req, sess.epost, "admin", "admin_bruker_slettet", "bruker:" + id, u.epost);
          return json({ ok: true });
        }

        if (p === "/admin/audit" && req.method === "GET") {
          const limit = Math.min(parseInt(url.searchParams.get("limit"), 10) || 100, 500);
          const conds = [], binds = [];
          for (const [param, col, op] of [["bruker", "bruker", "LIKE"], ["handling", "handling", "="],
                                          ["fra", "ts", ">="], ["til", "ts", "<="]]) {
            const v = url.searchParams.get(param);
            if (v) { conds.push(`${col} ${op} ?`); binds.push(op === "LIKE" ? `%${v}%` : v); }
          }
          const where = conds.length ? "WHERE " + conds.join(" AND ") : "";
          const rows = (await env.DB.prepare(
            `SELECT ts, bruker, rolle, handling, objekt, detaljer, ip FROM audit_log ${where}
             ORDER BY ts DESC LIMIT ?`).bind(...binds, limit).all()).results;
          return json(rows);
        }
      }

      // ---------- v1-kompatibelt ----------
      if (p === "/subscribe" && req.method === "POST") {
        const { email, addresses } = await req.json();
        const e = (email || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const user = await upsertUser(env, e);
        await writeProfile(env, user.id, { addresses });
        await audit(env, req, e, null, "abonnement_v1", null, null);
        return json({ ok: true, email: e });
      }
      if (p === "/subscriptions" && req.method === "GET") {
        const e = (url.searchParams.get("email") || "").toLowerCase().trim();
        if (!EMAIL_RE.test(e)) return err("Ugyldig e-postadresse", 400);
        const u = await env.DB.prepare("SELECT id FROM brukere WHERE epost = ?").bind(e).first();
        if (!u) return json({ email: e, addresses: [] });
        const prof = await readProfile(env, u.id);
        return json({ email: e, addresses: prof.addresses.map(a => a.label) });
      }
      if (p === "/unsubscribe" && req.method === "POST") {
        const { email } = await req.json();
        const e = (email || "").toLowerCase().trim();
        const u = await env.DB.prepare("SELECT id FROM brukere WHERE epost = ?").bind(e).first();
        if (u) await env.DB.prepare("DELETE FROM folger WHERE bruker_id = ?").bind(u.id).run();
        await audit(env, req, e, null, "avmeldt_v1", null, null);
        return json({ ok: true });
      }

      // ---------- For varselutsending (GitHub Actions) ----------
      if (p === "/all" && req.method === "GET") {
        const auth = req.headers.get("Authorization") || "";
        if (auth !== "Bearer " + env.API_SECRET) return err("Uautorisert", 401);
        const users = (await env.DB.prepare("SELECT id, epost FROM brukere").all()).results;
        const out = {};
        for (const u of users) {
          const prof = await readProfile(env, u.id);
          const push = (await env.DB.prepare("SELECT data FROM push_abonnement WHERE bruker_id = ?")
            .bind(u.id).all()).results.map(r => JSON.parse(r.data));
          out[u.epost] = { ...prof, push };
        }
        return json(out);
      }

      // ---------- PDF-proxy ----------
      if (p === "/pdfproxy" && req.method === "GET") {
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
