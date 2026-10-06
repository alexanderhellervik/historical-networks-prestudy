# Licences

The repository combines code, the author's derived data, and data derived from third-party
sources. Each keeps the terms below. No map sheet or scan is included, apart from the 1 km worked-example tile and two legend
images given to the reading model (`data/worked_example/`). No FOLKNET population figure and no
other raw source file is included (see `SOURCES.md` for where to download them).

| Path | What | Licence and source terms |
|---|---|---|
| `src/`, `scripts/`, `tests/`, `prompts/`, `report/scripts/`, `Makefile`, `pyproject.toml` | code and the instructions given to the models | MIT (`LICENSE`) |
| `report/` (text, PDF, HTML, figures) | the report | MIT (`LICENSE`). Figures drawn from Lantmäteriet maps carry a Lantmäteriet credit; figures with OpenStreetMap data carry © OpenStreetMap contributors (ODbL) |
| `data/corridor_1890s/` | the models' answers for each 1890s tile, tile transforms, search state, repairs, `EDITS.md` | MIT (`LICENSE`). Read from Häradsekonomiska kartan (sheets of 1890–97; map origin: Lantmäteriet), which is out of copyright: Lantmäteriet states that maps older than 70 years are not covered by copyright and may be published freely |
| `data/haradskartan_sheets.parquet` | sheet footprints of the Häradsekonomiska kartan packages | MIT (`LICENSE`); describes Naturvårdsverket's packages of Lantmäteriet sheets (out of copyright, as above) |
| `data/modern_network/` | the corridor's road network from OpenStreetMap | ODbL 1.0, © OpenStreetMap contributors (`data/modern_network/LICENSE-ODbL.txt`) |
| `data/network_1960s/scored_edges.gpkg` | OpenStreetMap road geometry, scored against Ekonomiska kartan | ODbL 1.0, © OpenStreetMap contributors. The scores are derived from Ekonomiska kartan (Lantmäteriet open data, CC0) |
| `data/network_1960s/sheet_list.csv` | file names and checksums of the Ekonomiska kartan sheets used | MIT (`LICENSE`); the sheets themselves are Lantmäteriet open data under CC0 |
| `data/timetable_1963/` | the AI transcription of the 1963 timetable tables 161 and 161 a | Transcribed from Sveriges Kommunikationer 1963 nr 6 (SJ), from scans hosted by Timetable World (<https://timetableworld.com/>); credited to SJ 1963 and Timetable World and removed on request under the site's takedown policy |
| `data/vgj_1963/` | the VGJ railway: running times and station links | Running times transcribed from Sveriges Kommunikationer 1963 nr 6 (SJ), tables 161 and 161 a, from scans hosted by Timetable World (<https://timetableworld.com/>); credited to SJ 1963 and Timetable World and removed on request under the site's takedown policy. Station links refer to OpenStreetMap nodes, ODbL 1.0, © OpenStreetMap contributors |
| `data/worked_example/` | the worked example given to the reading model: the author's own tracing of one 1 km tile (`reference_t06.gpkg`), the tile as shown to the model, the same tile with the tracing drawn on it, the caption, and two legend images | MIT (`LICENSE`) for the tracing, caption and drawings. The map images are from Häradsekonomiska kartan and its legend (Lantmäteriet, Rikets allmänna kartverks arkiv), out of copyright as above |
| `data/domain.gpkg` | the corridor's parish geometry | Derived from Riksarkivet's historical GIS boundaries as packaged in histmaps; the boundary data are CC0. Released here under CC0 |
| `data/crosswalk_folknet_histmaps.csv` | FOLKNET unit names and codes matched to parish codes (keys only, no population values) | MIT (`LICENSE`) for the matching. FOLKNET states no licence; cite it as: Demografiska databasen, CEDAR, Umeå universitet (2022): "Folkmängdsdatabasen", https://dx.doi.org/10.17197/folknetdb_v01 |

Terms were checked on 6 October 2026:

- Lantmäteriet, conditions for publishing its maps:
  <https://www.lantmateriet.se/sv/geodata/vara-produkter/Villkor-och-avgifter/>
- Lantmäteriet, open data under CC0:
  <https://www.lantmateriet.se/en/geodata/our-products/open-data/>
- histmaps: <https://github.com/junkka/histmaps>
- FOLKNET: <https://doi.org/10.17197/folknetdb_v01>
- OpenStreetMap: <https://www.openstreetmap.org/copyright>
