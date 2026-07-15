# -*- coding: utf-8 -*-
"""Baker Kartverkets åpne bygningspunkter (Matrikkelen - Bygningspunkt, CC-BY) for
Kristiansand til data/eiendom/bygninger.json: [lat, lon, bygningstype, status, sefrak].
Kjøres ved behov (datasettet oppdateres daglig via Geonorge ATOM-feed)."""
import io
import json
import math
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

URL = ("https://nedlasting.geonorge.no/geonorge/Basisdata/MatrikkelenBygning/GML/"
       "Basisdata_4204_Kristiansand_25832_MatrikkelenBygning_GML.zip")


def utm32_til_wgs84(east, north):
    """EPSG:25832 (ETRS89/UTM32, GRS80) -> lat/lon. Standard invers transversal Mercator."""
    a, f = 6378137.0, 1 / 298.257222101
    k0, e0, n0 = 0.9996, 500000.0, 0.0
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    m = (north - n0) / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = ep2 * cos1 ** 2
    t1 = tan1 ** 2
    n1 = a / math.sqrt(1 - e2 * sin1 ** 2)
    r1 = a * (1 - e2) / (1 - e2 * sin1 ** 2) ** 1.5
    d = (east - e0) / (n1 * k0)
    lat = phi1 - (n1 * tan1 / r1) * (d ** 2 / 2
          - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
          + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = math.radians(9) + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
          + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    return round(math.degrees(lat), 6), round(math.degrees(lon), 6)


def main():
    print("Laster ned", URL)
    data = urllib.request.urlopen(URL, timeout=300).read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    navn = [n for n in zf.namelist() if n.lower().endswith(".gml")][0]
    print("Parser", navn, f"({zf.getinfo(navn).file_size/1e6:.1f} MB)")

    ut = []
    lokal = lambda t: t.split("}")[-1]
    for _, elem in ET.iterparse(io.BytesIO(zf.read(navn)), events=("end",)):
        if lokal(elem.tag) != "Bygning":
            continue
        btype = status = pos = None
        sefrak = False
        for e in elem.iter():
            t = lokal(e.tag)
            if t == "bygningstype":
                btype = e.text
            elif t == "bygningsstatus":
                status = e.text
            elif t == "harSefrakminne" and (e.text or "").lower() == "true":
                sefrak = True
            elif t == "pos" and pos is None:
                pos = e.text
        if btype and pos:
            c1, c2 = (float(x) for x in pos.split()[:2])
            east, north = (c1, c2) if c1 < c2 else (c2, c1)  # easting ~4e5, northing ~6.4e6
            lat, lon = utm32_til_wgs84(east, north)
            rad = [lat, lon, int(btype), status or ""]
            if sefrak:
                rad.append(1)
            ut.append(rad)
        elem.clear()

    with open("data/eiendom/bygninger.json", "w", encoding="utf-8") as f:
        json.dump(ut, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Skrev {len(ut)} bygninger til data/eiendom/bygninger.json")
    ref = [b for b in ut if abs(b[0] - 58.086786) < 1e-4 and abs(b[1] - 7.810924) < 1e-4]
    print("kontroll (Anderåsen 6, forventer type 111):", ref[:3])


if __name__ == "__main__":
    main()
