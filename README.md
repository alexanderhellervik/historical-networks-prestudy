# historical-networks-prestudy

This is a pilot feasibility study. It is not a historical accessibility series, and it
contains no analysis of population response. It demonstrates, for one corridor in western
Sweden (the former Västergötland–Göteborg railway between Sjövik, Anten, Gräfsnäs, Sollebrunn and
Nossebro, with Alingsås), that routable road networks can be built for three periods — the
1890s, the 1960s and today — and combined with population to give accessibility surfaces. The
1890s roads were read from 19th-century map sheets by AI models; the 1960s roads are modern
roads checked against the 1960s economic map; today's network is OpenStreetMap.

The short report can be read online at
<https://alexanderhellervik.github.io/historical-networks-prestudy/>, or as
[PDF](report/report.pdf) (source: [`report/report.md`](report/report.md); `report/report.html` is the
same page, published by `.github/workflows/pages.yml`). It summarises the road reading, its accuracy against one
tile checked by the author (starting from a machine reading), and the cost of scaling it to all of
southern Sweden.

## What is in this repository

| Path | What |
|---|---|
| `src/histnet/` | the pipeline: map tiles, model reading and review, tile search, stitching and repair, networks, population, accessibility |
| `prompts/` | the instructions given to the reading and reviewing models |
| `data/` | derived data that may be shared: the models' answers and repairs, from which `make demo` assembles the 1890s road lines (Lantmäteriet origin), the corridor's modern network (ODbL), the AI transcription of the 1963 timetable, the FOLKNET–parish crosswalk (keys only), and the worked example given to the reading model (the author's tracing of one tile, the tile, the legend images) |
| `report/` | the short report (`report.pdf`, `report.html`, source `report.md`), its figures and the scripts that build them (`make report`) |
| `SOURCES.md` | where to download the inputs that are **not** stored here (maps, population, boundaries, OpenStreetMap) |
| `LICENSES.md` | the terms for each path, and how to cite the sources |
| `CITATION.cff`, `.zenodo.json` | citation and archive metadata |
| `.github/workflows/pages.yml` | publishes the report to GitHub Pages |

## Rerun

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .            # includes hierx==0.1.1
# download the inputs listed in SOURCES.md into inputs/
make demo                   # networks and accessibility from the shipped answers; no model is called
make read-1890s             # optional: rerun the model reading (needs Codex and Claude Code access)
```

`make demo` rebuilds the three networks, disaggregates population to 250 m cells and computes the
accessibility surfaces with HierX 0.1.1, checked against exact shortest paths. From a fresh clone
it took 65 minutes on a 16-CPU, 64 GB machine (most of it exact values for the walking maps) and
reproduced the report's travel times, exact values and HierX values to the last digit.

## How to cite

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23187623.svg)](https://doi.org/10.5281/zenodo.23187623)

Cite the archive on Zenodo, or use `CITATION.cff`:

> Hellervik, A. (2026). *Routable historical road networks from old map sheets: a feasibility
> demonstration (historical-networks-prestudy)*. Zenodo. https://doi.org/10.5281/zenodo.23187623

That DOI always resolves to the latest version. To cite one exact version, use its own DOI from
the Zenodo record (version 1.0.0: https://doi.org/10.5281/zenodo.23187624).

## Licences

Code, report and the author's derived data: MIT (`LICENSE`). OpenStreetMap-derived files: ODbL 1.0
(© OpenStreetMap contributors). Parish geometry: CC0. The 1890s and 1960s road data have
Lantmäteriet as map origin (Häradsekonomiska kartan is out of copyright; Ekonomiska kartan is open
data under CC0). Per-path terms and the citation FOLKNET asks for are in
[`LICENSES.md`](LICENSES.md). No FOLKNET population data and no map sheet or raw source file is stored here, apart from the
1 km worked-example tile and two legend images given to the reading model (see `SOURCES.md`).
