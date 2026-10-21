"""Génère le modèle sémantique (TMDL) et les pages du rapport (PBIR) du projet Power BI.

À lancer Power BI Desktop fermé, après scripts/preparer_donnees.py :
    python scripts/generer_powerbi.py
Puis ouvrir Guichet_de _Banque.pbip et cliquer sur « Actualiser ».
"""
import csv
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DONNEES = ROOT / "donnees_powerbi"
MODELE = ROOT / "Guichet_de _Banque.SemanticModel" / "definition"
RAPPORT = ROOT / "Guichet_de _Banque.Report" / "definition"

SCHEMA_VISUEL = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json"
SCHEMA_PAGE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
SCHEMA_PAGES = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"


def vider(dossier):
    """Vide un dossier sans le supprimer (OneDrive verrouille souvent les dossiers eux-mêmes)."""
    dossier.mkdir(exist_ok=True)
    for element in dossier.iterdir():
        if element.is_dir():
            vider(element)
            try:
                element.rmdir()
            except OSError:
                pass
        else:
            element.unlink()


def guid():
    return str(uuid.uuid4())


def ident():
    return uuid.uuid4().hex[:20]


def q(nom):
    """Nom TMDL, entre quotes s'il contient autre chose que lettres/chiffres/_."""
    return nom if nom.replace("_", "").isalnum() and nom.isascii() else "'" + nom.replace("'", "''") + "'"


# --------------------------------------------------------------------------- modèle sémantique

TEXTE = {"code_commune", "nom_commune", "code_departement", "nom_departement", "code_region", "nom_region",
         "acces_agence", "type_osm", "enseigne", "indicateur", "unite", "source", "url"}
DECIMAL = {"lat", "lon", "distance_agence_km", "distance_dab_km", "valeur", "population_2023",
           "agences_bancaires_2020_insee", "agences_bancaires_2025_insee", "evolution_agences_2020_2025_pct",
           "agences_pour_10000_hab", "dab_osm_pour_10000_hab", "part_pop_sans_agence_commune_pct",
           "part_pop_desert_bancaire_pct", "population_sans_agence_commune", "population_desert_bancaire"}
CATEGORIE = {"lat": "Latitude", "lon": "Longitude"}
SANS_AGREGAT = TEXTE | {"lat", "lon", "id_osm", "drom", "evolution_agences_2020_2025_pct", "agences_pour_10000_hab",
                        "dab_osm_pour_10000_hab", "part_pop_sans_agence_commune_pct", "part_pop_desert_bancaire_pct",
                        "distance_agence_km", "distance_dab_km", "valeur"}

MESURES = {
    "communes": [
        ("Population", "SUM ( communes[population_2023] )", "#,0"),
        ("Communes", "COUNTROWS ( communes )", "#,0"),
        ("Agences", "SUM ( communes[agences_bancaires_2025] )", "#,0"),
        ("Agences pour 10 000 hab", "DIVIDE ( [Agences], [Population] ) * 10000", "0.00"),
        ("DAB (OSM)", "SUM ( communes[dab_osm] )", "#,0"),
        ("Communes sans agence", "CALCULATE ( [Communes], communes[agences_bancaires_2025] = 0 )", "#,0"),
        ("Part communes sans agence", "DIVIDE ( [Communes sans agence], [Communes] )", "0.0 %"),
        ("Population en désert bancaire",
         "VAR seuil = [Seuil désert (km) Valeur]\nRETURN\n    CALCULATE ( [Population], communes[distance_agence_km] >= seuil )",
         "#,0"),
        ("Communes en désert bancaire",
         "VAR seuil = [Seuil désert (km) Valeur]\nRETURN\n    CALCULATE ( [Communes], communes[distance_agence_km] >= seuil )",
         "#,0"),
        ("Part population en désert", "DIVIDE ( [Population en désert bancaire], [Population] )", "0.0 %"),
        ("Communes ayant perdu leur agence", "SUM ( communes[a_perdu_agence_2020_2025] )", "#,0"),
        ("Communes sans agence ni DAB", "SUM ( communes[sans_agence_ni_dab] )", "#,0"),
        ("Distance moyenne à une agence (km)",
         "DIVIDE (\n    SUMX ( communes, communes[distance_agence_km] * communes[population_2023] ),\n    [Population]\n)",
         "0.0"),
    ],
}

TABLES = {  # table -> fichier CSV
    "communes": "communes.csv",
    "departements": "departements.csv",
    "dab_osm": "dab_osm.csv",
    "references_bdf": "references_banque_de_france.csv",
}


def type_colonne(nom):
    if nom in TEXTE:
        return "string", "type text"
    if nom in DECIMAL:
        return "double", "type number"
    return "int64", "Int64.Type"


def indenter(texte, tabs):
    return "\n".join("\t" * tabs + ligne for ligne in texte.splitlines())


def table_csv(nom, fichier):
    with open(DONNEES / fichier, encoding="utf-8-sig") as f:
        colonnes = next(csv.reader(f))
    lignes = [f"table {q(nom)}", f"\tlineageTag: {guid()}", ""]
    for nom_m, expr, fmt in MESURES.get(nom, []):
        if "\n" in expr:
            lignes += [f"\tmeasure {q(nom_m)} =", indenter(expr, 3)]
        else:
            lignes.append(f"\tmeasure {q(nom_m)} = {expr}")
        lignes += [f"\t\tformatString: {fmt}", f"\t\tlineageTag: {guid()}", ""]
    for c in colonnes:
        dt, _ = type_colonne(c)
        lignes += [f"\tcolumn {q(c)}", f"\t\tdataType: {dt}"]
        if c in CATEGORIE:
            lignes.append(f"\t\tdataCategory: {CATEGORIE[c]}")
        lignes += [f"\t\tlineageTag: {guid()}",
                   f"\t\tsummarizeBy: {'none' if c in SANS_AGREGAT else 'sum'}",
                   f"\t\tsourceColumn: {c}", "", "\t\tannotation SummarizationSetBy = Automatic", ""]
    types = ", ".join(f'{{"{c}", {type_colonne(c)[1]}}}' for c in colonnes)
    m = f"""let
    Source = Csv.Document(File.Contents(DossierDonnees & "{fichier}"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Entetes = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Types = Table.TransformColumnTypes(Entetes, {{{types}}}, "en-US")
in
    Types"""
    lignes += [f"\tpartition {q(nom)} = m", "\t\tmode: import", "\t\tsource =", indenter(m, 4), "",
               "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(lignes)


def table_seuil():
    return f"""table 'Seuil désert (km)'
\tlineageTag: {guid()}

\tmeasure 'Seuil désert (km) Valeur' = SELECTEDVALUE ( 'Seuil désert (km)'[Seuil désert (km)], 10 )
\t\tformatString: 0
\t\tlineageTag: {guid()}

\tcolumn 'Seuil désert (km)'
\t\tformatString: 0
\t\tlineageTag: {guid()}
\t\tsummarizeBy: none
\t\tsourceColumn: [Value]

\t\textendedProperty ParameterMetadata =
\t\t\t\t{{
\t\t\t\t  "version": 0
\t\t\t\t}}

\t\tannotation SummarizationSetBy = User

\tpartition 'Seuil désert (km)' = calculated
\t\tmode: import
\t\tsource = GENERATESERIES ( 3, 20, 1 )

\tannotation PBI_Id = {uuid.uuid4().hex}
"""


def ecrire_modele():
    dossier_tables = MODELE / "tables"
    vider(dossier_tables)
    for nom, fichier in TABLES.items():
        (dossier_tables / f"{nom}.tmdl").write_text(table_csv(nom, fichier), encoding="utf-8")
    (dossier_tables / "Seuil désert (km).tmdl").write_text(table_seuil(), encoding="utf-8")

    chemin = str(DONNEES) + "\\"
    (MODELE / "expressions.tmdl").write_text(
        f'expression DossierDonnees = "{chemin}" meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]\n'
        f"\tlineageTag: {guid()}\n\n\tannotation PBI_ResultType = Text\n", encoding="utf-8")

    (MODELE / "relationships.tmdl").write_text(
        f"relationship {guid()}\n\tfromColumn: communes.code_departement\n\ttoColumn: departements.code_departement\n\n"
        f"relationship {guid()}\n\tfromColumn: dab_osm.code_commune\n\ttoColumn: communes.code_commune\n",
        encoding="utf-8")

    ordre = json.dumps(["DossierDonnees", *TABLES], ensure_ascii=False)
    (MODELE / "model.tmdl").write_text(
        "model Model\n\tculture: fr-FR\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: fr-FR\n\tvalueFilterBehavior: independent\n\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
        f"annotation PBI_QueryOrder = {ordre}\n\n"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
        'annotation PBI_ProTooling = ["DevMode"]\n\n'
        + "".join(f"ref table {q(t)}\n" for t in [*TABLES, "Seuil désert (km)"])
        + "\nref cultureInfo fr-FR\n", encoding="utf-8")


# --------------------------------------------------------------------------- rapport

def col(table, nom):
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": nom}},
            "queryRef": f"{table}.{nom}", "nativeQueryRef": nom}


def mes(table, nom):
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": nom}},
            "queryRef": f"{table}.{nom}", "nativeQueryRef": nom}


def somme(table, nom):
    return {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": table}},
                                                                "Property": nom}}, "Function": 0}},
            "queryRef": f"Sum({table}.{nom})", "nativeQueryRef": f"Somme de {nom}"}


def lit(valeur):
    return {"expr": {"Literal": {"Value": valeur}}}


def filtre_in(table, colonne, valeurs):
    return {"name": ident(), "type": "Categorical", "howCreated": "User",
            "field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": colonne}},
            "filter": {"Version": 2, "From": [{"Name": "t", "Entity": table, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": colonne}}],
                           "Values": [[{"Literal": {"Value": v}}] for v in valeurs]}}}]}}


def visuel(type_, x, y, w, h, roles=None, titre=None, objets=None, filtres=None, tri=None):
    v = {"visualType": type_}
    if roles:
        v["query"] = {"queryState": {r: {"projections": p} for r, p in roles.items()}}
        if tri:
            v["query"]["sortDefinition"] = {"sort": [{"field": tri["field"], "direction": "Descending"}],
                                            "isDefaultSort": False}
    if objets:
        v["objects"] = objets
    if titre:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": lit("true"), "text": lit(f"'{titre}'")}}]}
    c = {"$schema": SCHEMA_VISUEL, "name": ident(),
         "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0}, "visual": v}
    if filtres:
        c["filterConfig"] = {"filters": filtres}
    return c


def texte(x, y, w, h, contenu, taille="20pt", gras=True):
    style = {"fontSize": taille}
    if gras:
        style["fontWeight"] = "bold"
    return visuel("textbox", x, y, w, h, objets={"general": [{"properties": {
        "paragraphs": [{"textRuns": [{"value": ligne, "textStyle": style}]} for ligne in contenu.split("\n")]}}]})


def carte_kpi(x, y, mesure, titre):
    return visuel("card", x, y, 300, 130, {"Values": [mes("communes", mesure)]}, titre=titre)


def segment(x, y, w, projection, titre, unique=False):
    objets = {"data": [{"properties": {"mode": lit("'Dropdown'")}}]}
    if unique:
        objets["selection"] = [{"properties": {"singleSelect": lit("true")}}]
    return visuel("slicer", x, y, w, 90, {"Values": [projection]}, titre=titre, objets=objets)


SEUIL = col("Seuil désert (km)", "Seuil désert (km)")


def pages():
    vue_nationale = [
        texte(20, 10, 1880, 70, "Déserts bancaires en France : l'accès aux agences en 2025", "24pt"),
        carte_kpi(20, 95, "Agences", "Agences bancaires (BPE 2025)"),
        carte_kpi(340, 95, "Communes sans agence", "Communes sans agence"),
        carte_kpi(660, 95, "Population en désert bancaire", "Habitants en désert bancaire"),
        carte_kpi(980, 95, "Part population en désert", "Part de la population en désert"),
        carte_kpi(1300, 95, "Communes ayant perdu leur agence", "Communes ayant perdu leur agence (2020-2025)"),
        segment(1620, 95, 280, SEUIL, "Seuil du désert bancaire (km)", unique=True),
        visuel("shapeMap", 20, 245, 1100, 815,
               {"Category": [col("departements", "code_departement")],
                "Y": [mes("communes", "Part population en désert")]},
               titre="Part de la population à plus du seuil d'une agence, par département"),
        visuel("clusteredBarChart", 1140, 245, 760, 815,
               {"Category": [col("departements", "nom_departement")],
                "Y": [mes("communes", "Part population en désert")]},
               titre="Départements les plus touchés",
               tri=mes("communes", "Part population en désert")),
    ]
    communes = [
        texte(20, 10, 1880, 70, "Zoom communal : communes éloignées d'une agence", "24pt"),
        segment(20, 90, 420, col("communes", "nom_region"), "Région"),
        segment(460, 90, 420, col("communes", "nom_departement"), "Département"),
        segment(900, 90, 300, SEUIL, "Seuil du désert bancaire (km)", unique=True),
        visuel("azureMap", 20, 195, 1300, 865,
               {"Category": [col("communes", "nom_commune")],
                "Y": [col("communes", "lat")], "X": [col("communes", "lon")],
                "Size": [mes("communes", "Population")],
                "Series": [col("communes", "acces_agence")]},
               titre="Communes à plus de 5 km d'une agence (taille = population)",
               filtres=[filtre_in("communes", "acces_agence",
                                  ["'3 - Agence entre 5 et 10 km'", "'4 - Désert bancaire (plus de 10 km)'"])]),
        visuel("donutChart", 1340, 195, 560, 400,
               {"Category": [col("communes", "acces_agence")], "Y": [mes("communes", "Population")]},
               titre="Population selon la distance à une agence"),
        visuel("tableEx", 1340, 610, 560, 450,
               {"Values": [col("communes", "nom_commune"), col("communes", "nom_departement"),
                           mes("communes", "Population"), somme("communes", "distance_agence_km")]},
               titre="Plus grandes communes en désert bancaire",
               filtres=[filtre_in("communes", "desert_bancaire", ["1L"])],
               tri=mes("communes", "Population")),
    ]
    dab = [
        texte(20, 10, 1880, 70, "Distributeurs de billets (OpenStreetMap) et repères Banque de France", "24pt"),
        carte_kpi(20, 95, "DAB (OSM)", "DAB recensés dans OpenStreetMap"),
        carte_kpi(340, 95, "Communes sans agence ni DAB", "Communes sans agence ni DAB"),
        segment(660, 95, 420, col("communes", "nom_departement"), "Département"),
        visuel("azureMap", 20, 245, 1240, 815,
               {"Y": [col("dab_osm", "lat")], "X": [col("dab_osm", "lon")],
                "Series": [col("dab_osm", "type_osm")]},
               titre="Distributeurs automatiques de billets"),
        visuel("tableEx", 1280, 245, 620, 560,
               {"Values": [col("references_bdf", "indicateur"), somme("references_bdf", "valeur"),
                           col("references_bdf", "unite")]},
               titre="Repères officiels Banque de France (fin 2025)"),
        texte(1280, 820, 620, 240,
              "OpenStreetMap recense environ 22 000 DAB, contre environ 40 000 en réalité : la carte "
              "montre la couverture connue, pas l'exhaustivité. Les chiffres officiels de la Banque de "
              "France donnent l'ordre de grandeur réel.", "11pt", gras=False),
    ]
    evolution = [
        texte(20, 10, 1880, 70, "Évolution du réseau d'agences 2020 → 2025", "24pt"),
        carte_kpi(20, 95, "Communes ayant perdu leur agence", "Communes ayant perdu leur agence"),
        segment(340, 95, 420, col("communes", "nom_region"), "Région"),
        visuel("clusteredBarChart", 20, 245, 940, 815,
               {"Category": [col("departements", "nom_departement")],
                "Y": [somme("departements", "evolution_agences_2020_2025_pct")]},
               titre="Évolution du nombre d'agences 2020-2025 (%, INSEE)",
               tri=somme("departements", "evolution_agences_2020_2025_pct")),
        visuel("tableEx", 980, 245, 920, 600,
               {"Values": [col("communes", "nom_commune"), col("communes", "nom_departement"),
                           mes("communes", "Population"), somme("communes", "distance_agence_km")]},
               titre="Communes qui avaient une agence en 2020 et n'en ont plus en 2025",
               filtres=[filtre_in("communes", "a_perdu_agence_2020_2025", ["1L"])],
               tri=mes("communes", "Population")),
        texte(980, 860, 920, 200,
              "Prudence : l'INSEE diffuse le code « Banque, caisse d'épargne » avec réserve et déconseille "
              "les comptages en évolution aux échelons fins. Certaines hausses départementales reflètent des "
              "changements d'enregistrement plutôt que de vraies ouvertures.", "11pt", gras=False),
    ]
    return [("Vue nationale", vue_nationale), ("Communes", communes),
            ("Distributeurs", dab), ("Évolution 2020-2025", evolution)]


def ecrire_rapport():
    dossier = RAPPORT / "pages"
    vider(dossier)
    noms = []
    for titre, visuels in pages():
        nom = ident()
        noms.append(nom)
        (dossier / nom / "visuals").mkdir(parents=True)
        (dossier / nom / "page.json").write_text(json.dumps(
            {"$schema": SCHEMA_PAGE, "name": nom, "displayName": titre,
             "displayOption": "FitToPage", "height": 1080, "width": 1920}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        for v in visuels:
            (dossier / nom / "visuals" / v["name"]).mkdir()
            (dossier / nom / "visuals" / v["name"] / "visual.json").write_text(
                json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    (dossier / "pages.json").write_text(json.dumps(
        {"$schema": SCHEMA_PAGES, "pageOrder": noms, "activePageName": noms[0]}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ecrire_modele()
    ecrire_rapport()
    print("Modèle et rapport générés. Ouvrir Guichet_de _Banque.pbip puis cliquer sur « Actualiser ».")
