PY ?= python

.PHONY: help demo osm nationwide read-1890s network-1960s figures report test lint

help:
	@echo "make demo        networks and accessibility maps from the shipped data (no model calls)"
	@echo "make osm         road extract from inputs/osm/sweden-latest.osm.pbf (for read-1890s, network-1960s, nationwide)"
	@echo "make nationwide  sheet coverage and the tile-count estimate for southern Sweden"
	@echo "make read-1890s  rerun the 1890s reading (needs Codex and Claude Code access; hours)"
	@echo "make network-1960s  rescore the 1960s sheets from inputs/ekonomiska_kartan"
	@echo "make figures     pipeline and network figures for the report (after make demo)"
	@echo "make report      report/report.html and report/report.pdf from report/report.md (pip install -e .[report])"
	@echo "make test        unit tests"

demo:
	$(PY) -m histnet.demo

osm:
	mkdir -p work
	ogr2ogr -f GPKG work/osm_highways.gpkg inputs/osm/sweden-latest.osm.pbf lines \
	  -nln highways -t_srs EPSG:3006 -select osm_id,highway \
	  -where "highway IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link','secondary','secondary_link','tertiary','tertiary_link','unclassified','residential','track','service','living_street')"

nationwide:
	$(PY) -m histnet.nationwide sheets
	$(PY) -m histnet.nationwide estimate

read-1890s:
	$(PY) -m histnet.corridor mosaic
	$(PY) -m histnet.corridor run
	$(PY) -m histnet.corridor fill-core
	$(PY) -m histnet.corridor assemble
	$(PY) -m histnet.repair run
	$(PY) -m histnet.cutrepair run

network-1960s:
	$(PY) -m histnet.network1960s score
	$(PY) -m histnet.network1960s rescore-absent
	$(PY) -m histnet.network1960s network

figures:
	$(PY) report/scripts/pipeline_figure.py
	$(PY) report/scripts/network_figure.py

report:
	$(PY) report/scripts/build_report.py

test:
	$(PY) -m pytest -q

lint:
	ruff check src tests && ruff format --check src tests
