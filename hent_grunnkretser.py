# -*- coding: utf-8 -*-
"""Baker grunnkrets-choropleth for Kristiansand til data/eiendom/grunnkretser.geojson:
per grunnkrets -> folketall + befolkningstetthet + estimert prisnivå (kr/m²).

Kilder (åpne, ingen nøkkel):
  - Polygoner: Geonorge Basisdata Grunnkretser (GeoJSON, EPSG:4258 = lon/lat)
  - Befolkning: SSB tabell 04317 (nyeste år)
  - Prisnivå:   SSB tabell 14737 (boligverdimodellen) -> estimert kr/m² for en
                referanse-enebolig (120 m², 20-34 år) der kun grunnkretskoeffisienten
                varierer. Validert mot SSBs kommune-kr/m² (enebolig ~31 857).
"""
import io, json, math, urllib.request, zipfile

POLY_URL = ("https://nedlasting.geonorge.no/geonorge/Basisdata/Grunnkretser/GeoJSON/"
            "Basisdata_4204_Kristiansand_4258_Grunnkretser_GeoJSON.zip")
REF_AREAL = 120.0  # m² referansebolig


def ssb(tbl, query):
    req = urllib.request.Request(f"https://data.ssb.no/api/v0/no/table/{tbl}/",
        headers={"Content-Type": "application/json", "User-Agent": "Byggesak/1.0"},
        data=json.dumps({"query": query, "response": {"format": "json-stat2"}}).encode())
    return json.load(urllib.request.urlopen(req, timeout=120))


def poly_areal_km2(geom):
    """Flateareal (km²) fra GeoJSON Polygon/MultiPolygon i lon/lat (ekvirektangulær)."""
    def ring_area(ring, lat0):
        f = math.radians(lat0)
        mx = 111412.84 * math.cos(f) - 93.5 * math.cos(3 * f)
        my = 111132.92 - 559.82 * math.cos(2 * f)
        s = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]; x2, y2 = ring[i + 1]
            s += (x1 * mx) * (y2 * my) - (x2 * mx) * (y1 * my)
        return abs(s / 2)
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    total = 0.0
    for poly in polys:
        lat0 = poly[0][0][1]
        total += ring_area(poly[0], lat0) - sum(ring_area(h, lat0) for h in poly[1:])
    return total / 1e6


def main():
    print("Laster grunnkrets-polygoner …")
    req = urllib.request.Request(POLY_URL, headers={"User-Agent": "Byggesak/1.0"})
    data = urllib.request.urlopen(req, timeout=180).read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    navn = [n for n in zf.namelist() if n.lower().endswith(".geojson") and "grense" not in n.lower()][0]
    gj = json.loads(zf.read(navn).decode("utf-8-sig"))
    print(f"  {len(gj['features'])} polygoner")

    print("Henter befolkning (SSB 04317) …")
    d = ssb("04317", [
        {"code": "Grunnkretser", "selection": {"filter": "all", "values": ["4204*"]}},
        {"code": "Tid", "selection": {"filter": "top", "values": ["1"]}},
    ])
    gks = list(d["dimension"]["Grunnkretser"]["category"]["index"].keys())
    aar = list(d["dimension"]["Tid"]["category"]["label"].values())[0]
    befolkning = {gk: d["value"][i] for i, gk in enumerate(gks)}

    print("Henter prisnivå (SSB 14737, boligverdimodell) …")
    d2 = ssb("14737", [
        {"code": "Grunnkretser", "selection": {"filter": "all", "values": ["4204*"]}},
        {"code": "Boligtype", "selection": {"filter": "item", "values": ["01"]}},
        {"code": "Boligalder", "selection": {"filter": "item", "values": ["3"]}},
    ])
    gks2 = list(d2["dimension"]["Grunnkretser"]["category"]["index"].keys())
    cc = list(d2["dimension"]["ContentsCode"]["category"]["index"].keys())
    nc = len(cc)
    vals = d2["value"]
    def coef(gk, name):
        gi = gks2.index(gk)
        return vals[gi * nc + cc.index(name)]
    kvmpris = {}
    for gk in gks2:
        try:
            k = coef(gk, "Konstantledd"); lg = coef(gk, "Log"); gkk = coef(gk, "GrKretsKoeff")
            kf = coef(gk, "KorrFaktor"); ak = coef(gk, "AldersKoeff")
            if None in (k, lg, gkk, kf, ak):
                continue
            kvmpris[gk] = round(math.exp(k + lg * math.log(REF_AREAL) + gkk + ak) * kf)
        except Exception:
            pass

    n = 0
    for f in gj["features"]:
        gk = f["properties"].get("grunnkretsnummer")
        areal = poly_areal_km2(f["geometry"])
        pop = befolkning.get(gk)
        pris = kvmpris.get(gk)
        f["properties"] = {
            "gk": gk,
            "navn": f["properties"].get("grunnkretsnavn"),
            "folketall": pop,
            "areal_km2": round(areal, 3),
            "tetthet": round(pop / areal) if pop and areal > 0.01 else None,
            "kvmpris": pris,
        }
        if pop is not None or pris is not None:
            n += 1
    # dropp den store grensefila-geometrien vi ikke trenger: behold kun features m/data
    with open("data/eiendom/grunnkretser.geojson", "w", encoding="utf-8") as fh:
        json.dump(gj, fh, ensure_ascii=False, separators=(",", ":"))
    prisverdier = [v for v in kvmpris.values()]
    print(f"Skrev grunnkretser.geojson ({n} med data). Befolkning {aar}, sum {sum(v for v in befolkning.values() if v)}.")
    print(f"Prisnivå kr/m²: min {min(prisverdier)}, median {sorted(prisverdier)[len(prisverdier)//2]}, maks {max(prisverdier)}")


if __name__ == "__main__":
    main()
