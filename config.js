// Konfigurasjon for Byggesaker Kristiansand.
// apiBase/pdfProxy peker på Cloudflare-workeren (worker/varsler-worker.js).
window.BYGGESAK_CONFIG = {
  apiBase: "https://byggesak-api.per-732.workers.dev",
  pdfProxy: "https://byggesak-api.per-732.workers.dev/pdfproxy",
  vapidPublicKey: "BFgGLzdkvDtQ0pF91KQwPsdX5RwUdEDH3MCLffv2F4phdqwe-i4o659Ha1rmZ0_5U5IOdf6Fq489E_UW22JBqYo",
};
