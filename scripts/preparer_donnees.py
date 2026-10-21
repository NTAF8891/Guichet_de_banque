"""Prépare les tables Power BI du projet « Déserts bancaires ».

Sources :
- INSEE, API Melodi (ouverte, sans clé) :
    DS_BPE            -> nombre d'agences bancaires (A203) et points postaux par commune, 2025
    DS_BPE_EVOLUTION  -> nombre d'agences 2020 / 2025 par département (comparable dans le temps)
    DS_POPULATIONS_REFERENCE -> population municipale 2023 par commune
- INSEE, fichier local ds_bpe_evolution_com_2015_2025_geo_2026.zip
    -> présence/absence d'agence par commune en 2020 et 2025 (l'INSEE recommande
       la présence/absence plutôt que les comptages pour les évolutions communales)
- OpenStreetMap (Overpass) -> distributeurs automatiques de billets (DAB)
- communes-5m.geojson.gz   -> contours des communes

Sorties dans donnees_powerbi/ :
    communes.csv, departements.csv, dab_osm.csv, references_banque_de_france.csv,
    departements.topojson, communes.topojson (pour le visuel « Carte de formes »)

Lancement :  python scripts/preparer_donnees.py      (ajouter --refresh pour retélécharger)
"""
import csv
import gzip
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parent.parent
BRUT = ROOT / "donnees_brutes"
OUT = ROOT / "donnees_powerbi"
REFRESH = "--refresh" in sys.argv

MELODI = "https://api.insee.fr/melodi/data/"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "etude-deserts-bancaires/1.0"

# Seuil de « désert bancaire » : aucune agence dans la commune ni à moins de 10 km
# à vol d'oiseau (≈ 15 min de route, le critère utilisé par la Banque de France).
SEUIL_DESERT_KM = 10

TYPES_BPE = {
    "A203": "agences_bancaires",
    "A206": "bureaux_poste",
    "A207": "relais_poste",
    "A208": "agences_postales",
}

DROM = ["971", "972", "973", "974", "976"]


# --------------------------------------------------------------------------- téléchargements

def http_get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for essai in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:  # API publique : on retente sur erreur réseau ou 429
            if essai == 3:
                raise
            print(f"  nouvel essai ({e})")
            time.sleep(5 * (essai + 1))


def melodi(dataset, **params):
    """Toutes les observations d'un jeu Melodi (paginé), avec cache local."""
    cle = "_".join(f"{k}-{v}" for k, v in sorted(params.items()))
    cache = BRUT / f"{dataset}_{cle}.json"
    if cache.exists() and not REFRESH:
        return json.loads(cache.read_text(encoding="utf-8"))
    obs, page = [], 1
    while True:
        q = urllib.parse.urlencode({**params, "maxResult": 10000, "page": page})
        lot = json.loads(http_get(f"{MELODI}{dataset}?{q}"))["observations"]
        obs += lot
        if len(lot) < 10000:
            break
        page += 1
    cache.write_text(json.dumps(obs), encoding="utf-8")
    print(f"  {dataset} {params} : {len(obs)} observations")
    return obs


def code_geo(geo):  # "2026-COM-33063" -> "33063"
    return geo.rsplit("-", 1)[-1]


def valeur(o):
    return o["measures"]["OBS_VALUE_NIVEAU"].get("value")


def dab_openstreetmap():
    cache = BRUT / "osm_dab_france.json"
    if not cache.exists() or REFRESH:
        print("  téléchargement des DAB OpenStreetMap…")
        requete = """[out:json][timeout:600];
area["ISO3166-1"="FR"][admin_level=2]->.fr;
(node["amenity"="atm"](area.fr);node["atm"="yes"](area.fr););
out body;"""
        brut = http_get(OVERPASS, urllib.parse.urlencode({"data": requete}).encode())
        cache.write_bytes(brut)
    elements = json.loads(cache.read_text(encoding="utf-8"))["elements"]
    lignes = []
    for e in elements:
        t = e.get("tags", {})
        lignes.append({
            "id_osm": e["id"],
            "lat": e["lat"],
            "lon": e["lon"],
            "type_osm": "DAB isolé" if t.get("amenity") == "atm" else "DAB d'agence",
            "enseigne": t.get("brand") or t.get("operator") or t.get("name") or "",
        })
    return pd.DataFrame(lignes).drop_duplicates("id_osm")


# --------------------------------------------------------------------------- géographie

def charger_communes():
    with gzip.open(ROOT / "communes-5m.geojson.gz", "rt", encoding="utf-8") as f:
        features = json.load(f)["features"]
    lignes, geoms = [], []
    for ft in features:
        p = ft["properties"]
        # On écarte les arrondissements de Paris/Lyon/Marseille (on garde la commune)
        # et les collectivités d'outre-mer (975, 977, 978, 98x) absentes des sources INSEE.
        if "commune" in p or p["departement"].startswith(("975", "977", "978", "98")):
            continue
        g = shape(ft["geometry"])
        pt = g.representative_point()
        lignes.append({"code_commune": p["code"], "nom_commune": p["nom"],
                       "code_departement": p["departement"], "code_region": p["region"],
                       "lat": round(pt.y, 5), "lon": round(pt.x, 5)})
        geoms.append(g)
    return pd.DataFrame(lignes), geoms


def vecteurs_unitaires(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def distance_plus_proche_km(lat, lon, lat_cibles, lon_cibles):
    """Distance à vol d'oiseau (km) vers le point cible le plus proche."""
    arbre = cKDTree(vecteurs_unitaires(lat_cibles, lon_cibles))
    corde, _ = arbre.query(vecteurs_unitaires(lat, lon))
    return 2 * 6371.0 * np.arcsin(np.clip(corde / 2, 0, 1))


# --------------------------------------------------------------------------- TopoJSON

# Encarts des DROM, placés dans le golfe de Gascogne comme sur les cartes INSEE :
# département -> (longitude, latitude du centre de l'encart, hauteur en degrés)
ENCARTS = {
    "971": (-6.0, 47.6, 0.75),
    "972": (-6.0, 46.5, 0.65),
    "973": (-6.0, 45.0, 1.40),
    "974": (-6.0, 43.6, 0.70),
    "976": (-6.0, 42.6, 0.45),
}


def placer_en_encart(geom, dep, emprise):
    lon_c, lat_c, hauteur = ENCARTS[dep]
    minx, miny, maxx, maxy = emprise
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    s = hauteur / (maxy - miny)
    # Corrige l'étirement Mercator entre la latitude d'origine et celle de l'encart.
    sx = s * math.cos(math.radians(cy)) / math.cos(math.radians(lat_c))
    return shapely.transform(geom, lambda xy: np.column_stack(
        [lon_c + (xy[:, 0] - cx) * sx, lat_c + (xy[:, 1] - cy) * s]))


def ecrire_topojson(chemin, nom_objet, elements, quantification=100000):
    """elements : liste de (propriétés, géométrie). Arcs non partagés : simple et valide."""
    emprises = np.array([g.bounds for _, g in elements])
    x0, y0 = emprises[:, 0].min(), emprises[:, 1].min()
    kx = (emprises[:, 2].max() - x0) / (quantification - 1)
    ky = (emprises[:, 3].max() - y0) / (quantification - 1)
    arcs, geometries = [], []
    for props, g in elements:
        polygones = [g] if isinstance(g, Polygon) else list(getattr(g, "geoms", []))
        mp = []
        for poly in polygones:
            if not isinstance(poly, Polygon) or poly.is_empty:
                continue
            poly = orient(poly, sign=-1.0)  # anneau extérieur horaire, convention d3/Power BI
            anneaux = []
            for anneau in [poly.exterior, *poly.interiors]:
                q = np.rint((np.asarray(anneau.coords) - [x0, y0]) / [kx, ky]).astype(int)
                q = q[np.r_[True, np.any(np.diff(q, axis=0) != 0, axis=1)]]
                if len(q) < 4:
                    continue
                delta = np.vstack([q[:1], np.diff(q, axis=0)])
                arcs.append(delta.tolist())
                anneaux.append([len(arcs) - 1])
            if anneaux:
                mp.append(anneaux)
        geometries.append({"type": "MultiPolygon", "arcs": mp, "id": props["code"], "properties": props})
    topo = {"type": "Topology",
            "transform": {"scale": [kx, ky], "translate": [x0, y0]},
            "objects": {nom_objet: {"type": "GeometryCollection", "geometries": geometries}},
            "arcs": arcs}
    chemin.write_text(json.dumps(topo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  {chemin.name} : {chemin.stat().st_size / 1e6:.1f} Mo")


# --------------------------------------------------------------------------- traitement

def main():
    BRUT.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)

    print("1/6 Communes et contours")
    communes, geoms = charger_communes()

    print("2/6 Population et équipements (API INSEE Melodi)")
    pop = {code_geo(o["dimensions"]["GEO"]): valeur(o)
           for o in melodi("DS_POPULATIONS_REFERENCE", GEO="COM", POPREF_MEASURE="PMUN")}
    communes["population_2023"] = communes["code_commune"].map(pop)

    for type_equ, colonne in TYPES_BPE.items():
        n = {code_geo(o["dimensions"]["GEO"]): valeur(o)
             for o in melodi("DS_BPE", FACILITY_TYPE=type_equ, GEO="COM")}
        communes[colonne + "_2025"] = communes["code_commune"].map(n).fillna(0).astype(int)

    # Présence d'agence 2020 / 2025 (indicateurs 0/1 du fichier BPE évolution).
    presence = {}
    with zipfile.ZipFile(ROOT / "ds_bpe_evolution_com_2015_2025_geo_2026.zip") as z:
        with z.open("ds_bpe_evolution_com_2015_2025_geo_2026.csv") as f:
            for d in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"), delimiter=";"):
                if d["FACILITY_TYPE"] == "A203" and d["GEO_OBJECT"] == "COM" and d["OBS_VALUE"]:
                    presence[(d["GEO"], d["TIME_PERIOD"])] = int(float(d["OBS_VALUE"]))
    for an in ("2020", "2025"):
        communes[f"agence_presente_{an}"] = [presence.get((c, an), 0) for c in communes["code_commune"]]
    communes["a_perdu_agence_2020_2025"] = ((communes["agence_presente_2020"] == 1)
                                            & (communes["agence_presente_2025"] == 0)).astype(int)
    communes["a_gagne_agence_2020_2025"] = ((communes["agence_presente_2020"] == 0)
                                            & (communes["agence_presente_2025"] == 1)).astype(int)

    print("3/6 Distributeurs de billets (OpenStreetMap)")
    dab = dab_openstreetmap()
    arbre = shapely.STRtree(geoms)
    idx_pt, idx_com = arbre.query(shapely.points(dab["lon"], dab["lat"]), predicate="within")
    dab["code_commune"] = None
    dab.loc[dab.index[idx_pt], "code_commune"] = communes["code_commune"].values[idx_com]
    dab = dab.dropna(subset=["code_commune"])
    communes["dab_osm"] = communes["code_commune"].map(dab["code_commune"].value_counts()).fillna(0).astype(int)

    print("4/6 Distances et classement des communes")
    avec = communes[communes["agences_bancaires_2025"] > 0]
    communes["distance_agence_km"] = np.round(distance_plus_proche_km(
        communes["lat"], communes["lon"], avec["lat"], avec["lon"]), 1)
    communes.loc[communes["agences_bancaires_2025"] > 0, "distance_agence_km"] = 0.0
    communes["distance_dab_km"] = np.round(distance_plus_proche_km(
        communes["lat"], communes["lon"], dab["lat"], dab["lon"]), 1)
    communes.loc[communes["dab_osm"] > 0, "distance_dab_km"] = 0.0

    d = communes["distance_agence_km"]
    communes["acces_agence"] = np.select(
        [communes["agences_bancaires_2025"] > 0, d < 5, d < SEUIL_DESERT_KM],
        ["1 - Agence dans la commune", "2 - Agence à moins de 5 km", "3 - Agence entre 5 et 10 km"],
        "4 - Désert bancaire (plus de 10 km)")
    communes["desert_bancaire"] = (d >= SEUIL_DESERT_KM).astype(int)
    communes["sans_agence_ni_dab"] = ((communes["agences_bancaires_2025"] == 0)
                                      & (communes["dab_osm"] == 0)).astype(int)
    communes["points_postaux_2025"] = communes[["bureaux_poste_2025", "relais_poste_2025",
                                                "agences_postales_2025"]].sum(axis=1)

    print("5/6 Agrégats départementaux")
    with zipfile.ZipFile(ROOT / "ensemble.zip") as z:
        brut = z.read("donnees_departements.csv")
    try:
        texte = brut.decode("utf-8")
    except UnicodeDecodeError:
        texte = brut.decode("cp1252")
    noms = pd.read_csv(io.StringIO(texte), sep=";", dtype=str)[["DEP", "Département", "Région"]]
    noms.columns = ["code_departement", "nom_departement", "nom_region"]
    if "976" not in set(noms["code_departement"]):  # Mayotte absente des populations légales 2021
        noms.loc[len(noms)] = ["976", "Mayotte", "Mayotte"]
    communes = communes.merge(noms, on="code_departement", how="left")

    evol = {}
    for o in melodi("DS_BPE_EVOLUTION", FACILITY_TYPE="A203", GEO="DEP"):
        evol[(code_geo(o["dimensions"]["GEO"]), o["dimensions"]["TIME_PERIOD"])] = valeur(o)

    pop_sans = communes["population_2023"].where(communes["agences_bancaires_2025"] == 0, 0)
    pop_desert = communes["population_2023"].where(communes["desert_bancaire"] == 1, 0)
    deps = (communes.assign(pop_sans_agence=pop_sans, pop_desert=pop_desert,
                            sans_agence=(communes["agences_bancaires_2025"] == 0).astype(int))
            .groupby(["code_departement", "nom_departement", "nom_region"], as_index=False)
            .agg(population_2023=("population_2023", "sum"),
                 nb_communes=("code_commune", "count"),
                 agences_bancaires_2025=("agences_bancaires_2025", "sum"),
                 dab_osm=("dab_osm", "sum"),
                 points_postaux_2025=("points_postaux_2025", "sum"),
                 communes_sans_agence=("sans_agence", "sum"),
                 communes_desert_bancaire=("desert_bancaire", "sum"),
                 communes_ayant_perdu_agence=("a_perdu_agence_2020_2025", "sum"),
                 population_sans_agence_commune=("pop_sans_agence", "sum"),
                 population_desert_bancaire=("pop_desert", "sum")))
    deps["population_2023"] = deps["population_2023"].replace(0, np.nan)  # Mayotte : pas de population de référence
    deps["agences_bancaires_2020_insee"] =[evol.get((c, "2020")) for c in deps["code_departement"]]
    deps["agences_bancaires_2025_insee"] = [evol.get((c, "2025")) for c in deps["code_departement"]]
    deps["evolution_agences_2020_2025_pct"] = np.round(
        100 * (deps["agences_bancaires_2025_insee"] / deps["agences_bancaires_2020_insee"] - 1), 1)
    deps["agences_pour_10000_hab"] = np.round(1e4 * deps["agences_bancaires_2025"] / deps["population_2023"], 2)
    deps["dab_osm_pour_10000_hab"] = np.round(1e4 * deps["dab_osm"] / deps["population_2023"], 2)
    deps["part_pop_sans_agence_commune_pct"] = np.round(
        100 * deps["population_sans_agence_commune"] / deps["population_2023"], 1)
    deps["part_pop_desert_bancaire_pct"] = np.round(
        100 * deps["population_desert_bancaire"] / deps["population_2023"], 1)
    deps["drom"] = deps["code_departement"].isin(DROM).astype(int)

    colonnes = ["code_commune", "nom_commune", "code_departement", "nom_departement", "code_region",
                "nom_region", "population_2023", "lat", "lon", "agences_bancaires_2025",
                "agence_presente_2020", "agence_presente_2025", "a_perdu_agence_2020_2025",
                "a_gagne_agence_2020_2025", "bureaux_poste_2025", "relais_poste_2025",
                "agences_postales_2025", "points_postaux_2025", "dab_osm", "distance_agence_km",
                "distance_dab_km", "acces_agence", "desert_bancaire", "sans_agence_ni_dab"]
    communes[colonnes].to_csv(OUT / "communes.csv", index=False, encoding="utf-8-sig")
    deps.to_csv(OUT / "departements.csv", index=False, encoding="utf-8-sig")
    dab.to_csv(OUT / "dab_osm.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([
        ("Population à moins de 15 min d'un DAB (fin 2025, métropole)", 98.6, "%"),
        ("Population à moins de 15 min d'un DAB ou point privatif (fin 2025)", 99.8, "%"),
        ("Temps moyen d'accès, communes sans point de retrait (2025)", 9.3, "min"),
        ("Évolution des sites équipés de DAB en 2023", -3.9, "%"),
        ("Évolution des sites équipés de DAB en 2024", -1.9, "%"),
        ("Évolution des sites équipés de DAB en 2025", -2.7, "%"),
        ("Communes disposant d'au moins un DAB", 6500, "communes"),
        ("Points privatifs de retrait d'espèces (fin 2025)", 30057, "points"),
    ], columns=["indicateur", "valeur", "unite"]).assign(
        source="Banque de France, Accès du public aux espèces, état des lieux à fin 2025",
        url="https://www.banque-france.fr/en/publications-and-statistics/publications/public-access-cash-update-report-year-end-2025",
    ).to_csv(OUT / "references_banque_de_france.csv", index=False, encoding="utf-8-sig")

    print("6/6 Fonds de carte TopoJSON")
    communes_geo = []
    par_dep = {}
    emprise_drom = {dep: shapely.union_all([g for g, c in zip(geoms, communes["code_departement"]) if c == dep]).bounds
                    for dep in DROM}
    for g, row in zip(geoms, communes[["code_commune", "nom_commune", "code_departement"]].itertuples(index=False)):
        if row.code_departement in DROM:
            g = placer_en_encart(g, row.code_departement, emprise_drom[row.code_departement])
        par_dep.setdefault(row.code_departement, []).append(g)
        communes_geo.append(({"code": row.code_commune, "nom": row.nom_commune, "dep": row.code_departement},
                             g.simplify(0.004, preserve_topology=True)))
    noms_dep = dict(zip(deps["code_departement"], deps["nom_departement"]))
    deps_geo = [({"code": dep, "nom": noms_dep.get(dep, "")},
                 shapely.coverage_union_all(gs).simplify(0.01, preserve_topology=True))
                for dep, gs in sorted(par_dep.items())]
    ecrire_topojson(OUT / "departements.topojson", "departements", deps_geo)
    ecrire_topojson(OUT / "communes.topojson", "communes", communes_geo, quantification=20000)

    # Contrôles
    print("\nContrôles")
    print(f"  communes : {len(communes)} | sans population : {communes['population_2023'].isna().sum()}")
    print(f"  agences 2025 rattachées : {communes['agences_bancaires_2025'].sum()} (INSEE France : "
          f"{sum(v or 0 for (c, a), v in evol.items() if a == '2025'):.0f})")
    print(f"  DAB OSM rattachés à une commune : {len(dab)}")
    print(f"  communes ayant perdu leur agence 2020-2025 : {communes['a_perdu_agence_2020_2025'].sum()}"
          f" | en ayant gagné une : {communes['a_gagne_agence_2020_2025'].sum()}")
    print(f"  communes en désert bancaire : {communes['desert_bancaire'].sum()}"
          f" ({pop_desert.sum():,.0f} habitants)")


if __name__ == "__main__":
    main()
