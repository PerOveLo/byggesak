// Konfigurasjon for Byggesaker Kristiansand.
// apiBase/pdfProxy peker på Cloudflare-workeren (worker/varsler-worker.js).
window.BYGGESAK_CONFIG = {
  apiBase: "https://byggesak-api.per-732.workers.dev",
  pdfProxy: "https://byggesak-api.per-732.workers.dev/pdfproxy",
  vapidPublicKey: "",  // valgfritt: web-push (npx web-push generate-vapid-keys)
};
