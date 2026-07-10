-- D1-skjema for Byggesaker Kristiansand (speiler fremtidig Postgres-modell).
-- Kjøres med: wrangler d1 execute byggesak --file=schema.sql --remote

CREATE TABLE IF NOT EXISTS brukere (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  epost TEXT UNIQUE NOT NULL,
  rolle TEXT NOT NULL DEFAULT 'bruker',     -- bruker | admin
  tier TEXT NOT NULL DEFAULT 'gratis',      -- gratis | privat | bedrift
  opprettet TEXT NOT NULL,
  sist_innlogget TEXT
);

CREATE TABLE IF NOT EXISTS sesjoner (
  token TEXT PRIMARY KEY,
  bruker_id INTEGER NOT NULL REFERENCES brukere(id),
  opprettet TEXT NOT NULL,
  utloper TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_tokens (
  token TEXT PRIMARY KEY,
  epost TEXT NOT NULL,
  utloper TEXT NOT NULL
);

-- Følger: adresse | sak | firma | sb | omraade (punkt+radius)
CREATE TABLE IF NOT EXISTS folger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bruker_id INTEGER NOT NULL REFERENCES brukere(id),
  slag TEXT NOT NULL,
  verdi TEXT NOT NULL DEFAULT '',
  nivaa TEXT NOT NULL DEFAULT 'alt',        -- alt | vedtak | nye
  lat REAL, lon REAL, radius_m INTEGER,
  opprettet TEXT NOT NULL,
  UNIQUE(bruker_id, slag, verdi)
);

CREATE TABLE IF NOT EXISTS notater (
  bruker_id INTEGER NOT NULL REFERENCES brukere(id),
  saksnr TEXT NOT NULL,
  tekst TEXT NOT NULL,
  oppdatert TEXT NOT NULL,
  PRIMARY KEY (bruker_id, saksnr)
);

CREATE TABLE IF NOT EXISTS push_abonnement (
  endpoint TEXT PRIMARY KEY,
  bruker_id INTEGER NOT NULL REFERENCES brukere(id),
  data TEXT NOT NULL,
  opprettet TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS innstillinger (
  bruker_id INTEGER PRIMARY KEY REFERENCES brukere(id),
  ukesammendrag INTEGER NOT NULL DEFAULT 0
);

-- Revisjonslogg: append-only (triggere blokkerer endring/sletting), jf. GDPR art. 32/5(2)
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  bruker TEXT,
  rolle TEXT,
  handling TEXT NOT NULL,
  objekt TEXT,
  detaljer TEXT,
  ip TEXT,
  user_agent TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS audit_bruker ON audit_log(bruker);
CREATE INDEX IF NOT EXISTS audit_handling ON audit_log(handling);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log er append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log er append-only'); END;
