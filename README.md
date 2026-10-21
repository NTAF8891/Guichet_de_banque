# Déserts bancaires : réseau d'agences et territoires

Carte des agences bancaires et des distributeurs de billets par commune et par département,
croisée avec la population, pour repérer les « déserts bancaires ».

## 1. Régénérer les données

```powershell
python -m pip install --user pandas numpy shapely scipy
python scripts\preparer_donnees.py            # --refresh pour retélécharger les sources
```

Les téléchargements sont mis en cache dans `donnees_brutes/` et les tables Power BI sont écrites dans `donnees_powerbi/`.

| Fichier | Grain | Contenu |
|---|---|---|
| `communes.csv` | 1 ligne par commune (34 875) | population 2023, agences 2025, présence d'agence 2020/2025, points postaux, DAB, distances, classe d'accès |
| `departements.csv` | 1 ligne par département (101) | agrégats, agences 2020→2025 (INSEE), ratios pour 10 000 hab., part de population en désert |
| `dab_osm.csv` | 1 ligne par DAB (22 354) | lat/lon, type, enseigne, commune |
| `references_banque_de_france.csv` | indicateurs nationaux | chiffres officiels BdF fin 2025 (cartes KPI) |
| `departements.topojson`, `communes.topojson` | fonds de carte | pour le visuel *Carte de formes*, avec les DROM en encart |

## 2. Définitions

- **Agence** : code BPE A203 « Banque, caisse d'épargne » (INSEE, 1er janvier 2025).
- **Distance à l'agence** : distance à vol d'oiseau entre le centre de la commune et le centre de la commune la plus proche qui a au moins une agence (0 si la commune en a une).
- **Désert bancaire** : aucune agence à moins de **10 km**, soit environ 15 min de route, le seuil utilisé par la Banque de France. Le seuil est modifiable via `SEUIL_DESERT_KM` dans le script ou via un paramètre de simulation dans Power BI (voir plus bas).
- **A perdu son agence** : commune qui avait au moins une agence en 2020 et n'en a plus en 2025. Cet indicateur de présence/absence est celui que recommande l'INSEE pour les évolutions à l'échelle communale.

## 3. Modèle Power BI

1. *Obtenir les données → Texte/CSV* : importer les 4 CSV. Mettre `code_commune` et `code_departement` en **Texte**, sinon on perd le zéro de « 01001 » et la Corse (2A/2B).
2. Relations :
   - `departements[code_departement]` 1 → * `communes[code_departement]`
   - `communes[code_commune]` 1 → * `dab_osm[code_commune]`
3. Catégorie de données : `communes[lat]` = Latitude, `communes[lon]` = Longitude (idem pour `dab_osm`).

### Mesures DAX

```dax
Population = SUM ( communes[population_2023] )
Agences = SUM ( communes[agences_bancaires_2025] )
DAB (OSM) = SUM ( communes[dab_osm] )
Agences pour 10 000 hab = DIVIDE ( [Agences], [Population] ) * 10000

Communes sans agence = CALCULATE ( COUNTROWS ( communes ), communes[agences_bancaires_2025] = 0 )
Part communes sans agence = DIVIDE ( [Communes sans agence], COUNTROWS ( communes ) )

Population en désert bancaire =
    CALCULATE ( [Population], communes[distance_agence_km] >= [Seuil désert (km) Valeur] )
Part population en désert = DIVIDE ( [Population en désert bancaire], [Population] )

Communes ayant perdu leur agence = SUM ( communes[a_perdu_agence_2020_2025] )
Distance moyenne pondérée (km) =
    DIVIDE ( SUMX ( communes, communes[distance_agence_km] * communes[population_2023] ), [Population] )
```

Paramètre de simulation : *Modélisation → Nouveau paramètre → Plage numérique*, nommé `Seuil désert (km)`, de 3 à 20, pas de 1, valeur par défaut 10. Le curseur met à jour la carte et les KPI en direct, et c'est l'élément interactif le plus convaincant en démonstration.

## 4. Pages du rapport

**Page 1 : Vue nationale (départements)**
- *Carte de formes* : Paramètres de la carte → Type = Carte personnalisée → `departements.topojson`. Champ *Emplacement* = `code_departement` (correspondance sur la propriété `code`). Saturation des couleurs = `Part population en désert` ou `Agences pour 10 000 hab`.
- Cartes KPI : population en désert, communes sans agence, communes ayant perdu leur agence, et les chiffres BdF de `references_banque_de_france.csv` en bandeau de contexte.
- Graphique à barres : top 15 des départements par part de population en désert.

**Page 2 : Zoom communal**
- *Carte Azure Maps* : Latitude/Longitude = `communes[lat]`/`communes[lon]`, Taille = `Population`, Légende = `acces_agence`. Palette conseillée : 1 vert, 2 jaune, 3 orange, 4 rouge.
- Calque de bulles secondaire, ou seconde carte, avec `dab_osm` pour voir les DAB.
- Segment `nom_departement` et curseur du seuil.
- Alternative : *Carte de formes* avec `communes.topojson` (7,5 Mo, correspondance sur `code`) filtrée sur un département. C'est plus lent au niveau national.

**Page 3 : Fermetures 2020 → 2025**
- Carte des communes avec `a_perdu_agence_2020_2025 = 1`.
- Barres : `evolution_agences_2020_2025_pct` par département, avec l'avertissement ci-dessous en note.

## 5. Limites (à mentionner dans la publication)

- **La BPE sous-estime le nombre d'agences** : 23 986 en 2025. Elle recense les établissements déclarés au répertoire SIRENE avec accueil du public, pas chaque guichet. Elle sert surtout à mesurer la *présence* d'une agence dans une commune, moins à compter précisément les agences.
- **Les évolutions 2020 → 2025 sont fragiles** : l'INSEE diffuse le code A203 « avec réserve » et déconseille les comptages en évolution aux échelons fins. Certaines hausses départementales (Haute-Marne +67 %, Aveyron +53 %) reflètent des changements d'enregistrement plus que de vraies ouvertures. Il vaut mieux s'appuyer sur l'état des lieux 2025 et sur la présence/absence.
- **Les DAB OpenStreetMap sont incomplets** : environ 22 000 points contre environ 40 000 DAB réels. La couverture varie selon les contributeurs. La Banque de France ne publie pas ses données par commune, seulement une [carte interactive](https://banque-france.articque.com/share/display/carte-communes-point-acces-2025). Les chiffres officiels sont donc repris en contexte.
- **Distances à vol d'oiseau**, pas des temps de trajet routiers.
- **Mayotte** n'a pas de population de référence 2023 dans la source INSEE utilisée.

## Sources

- INSEE, Base permanente des équipements 2025 et BPE en évolution 2015-2020-2025, [API Melodi](https://api.insee.fr/melodi/) (jeux `DS_BPE`, `DS_BPE_EVOLUTION`)
- INSEE, Populations de référence 2023 (`DS_POPULATIONS_REFERENCE`)
- Banque de France, [Accès du public aux espèces, état des lieux à fin 2025](https://www.banque-france.fr/en/publications-and-statistics/publications/public-access-cash-update-report-year-end-2025)
- © contributeurs OpenStreetMap, licence ODbL (DAB)
- Contours des communes : `communes-5m.geojson`
