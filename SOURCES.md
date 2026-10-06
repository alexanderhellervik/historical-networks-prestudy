# Sources: what to download, and what is (not) in this repository

No raw source file is stored here. Download the inputs below into `inputs/` (paths shown), then
run `make demo`. Each source keeps its own terms (checked 6 October 2026; details in `LICENSES.md`):
Häradsekonomiska kartan is out of copyright (Lantmäteriet: maps older than 70 years); Ekonomiska
kartan is Lantmäteriet open data under CC0; histmaps boundary data are CC0; OpenStreetMap is ODbL
1.0; FOLKNET states no licence and asks to be cited as "Demografiska databasen, CEDAR, Umeå
universitet (2022): Folkmängdsdatabasen, https://dx.doi.org/10.17197/folknetdb_v01".

| Source | What it gives | Where to get it | In this repository |
|---|---|---|---|
| **Häradsekonomiska kartan** (1859–1934), georeferenced scans by Naturvårdsverket of Lantmäteriet sheets | the 1890s map read by the models | <https://geodata.naturvardsverket.se/nedladdning/haradskartan/> — packages `Haradskarta_33.zip` and `Haradskarta_42.zip` → `inputs/haradskartan/` | no, apart from the 1 km worked-example tile and two legend images given to the reading model (`data/worked_example/`; legend from Lantmäteriet's [teckenförklaring](https://www.lantmateriet.se/sv/kartor/vara-karttjanster/Historiska-kartor/Arkiven-som-ingar/Rikets-allmanna-kartverks-arkiv---RAK/contentassets/teckenforklaring-haradsekonomiska-kartan-1_20000.pdf) and the package's own legend page). Derived road lines are included and marked with Lantmäteriet origin; figures drawn from the maps carry a Lantmäteriet badge |
| **Ekonomiska kartan** 1:10 000 (Lantmäteriet), editions 1963–74 | the 1960s map against which today's roads are checked | Lantmäteriet open data, `ftp://download-opendata.lantmateriet.se/Ekonomiska_kartan/` — the 41 sheets of RT90 squares 7B, 7C, 8B, 8C listed in `data/network_1960s/sheet_list.csv` → `inputs/ekonomiska_kartan/` | no (pointer only). The scored road edges are included (OpenStreetMap geometry, ODbL); figures drawn from the maps carry a Lantmäteriet badge |
| **FOLKNET** population database (CEDAR, Umeå University), DOI [10.17197/folknetdb_v01](https://doi.org/10.17197/folknetdb_v01) | parish and town population, 1810–1990 | export the parish (församling) and town (stad) series for Älvsborgs and Skaraborgs län via the FOLKNET service → `inputs/folknet/` | no; only the unit keys (names, codes) in `data/crosswalk_folknet_histmaps.csv`, no population values |
| **histmaps** (Riksarkivet historical boundaries, packaged by J. Junkka) | parish polygons valid in 1890 | <https://github.com/junkka/histmaps> (`data/geom_sp.rda`, `geom_meta.rda`, `geom_relations.rda`) → `inputs/histmaps/` | no |
| **OpenStreetMap** (Geofabrik extract for Sweden) | today's road and rail network | <https://download.geofabrik.de/europe/sweden-latest.osm.pbf> → `inputs/osm/` | the derived corridor network only, under ODbL 1.0 (© OpenStreetMap contributors) |
| **HierX** 0.1.1 | the accessibility operator | `pip install hierx==0.1.1` | no (dependency) |
