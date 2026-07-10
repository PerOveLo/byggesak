// Konfigurasjon for offentlig hosting av byggesakskartet.
// Fyll inn URL-ene når Cloudflare Worker-en (worker/varsler-worker.js) er deployet.
// Tomme verdier = e-postvarsling lagres kun lokalt, og PDF-er åpnes i egen fane
// (unntatt på localhost, der server.py sin innebygde proxy brukes automatisk).
window.BYGGESAK_CONFIG = {
  apiBase: "",   // f.eks. "https://byggesak-varsler.<konto>.workers.dev"
  pdfProxy: "",  // f.eks. "https://byggesak-varsler.<konto>.workers.dev/pdfproxy"
};
