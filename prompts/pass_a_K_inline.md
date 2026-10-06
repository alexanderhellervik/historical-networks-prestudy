# Pass A, arm K — interpretation of one tile with the legend crop and a worked example

Informed-reading experiment (`experiments/informed_reading/plan.md`). Sent once per
(tile, model family) in a fresh context. The text below the line is `pass_a_L.md` with
one more paragraph after **Road and boundary symbols**: **Worked example**, which quotes
`data/informed_reading/example_caption.md` unchanged. Attachments, in this order:

1. `<tile_id>_bare.png` — the map face alone, 1000 x 1000 px, no annotation;
2. `<tile_id>_grid.png` — the same face with a 100-pixel grid and labelled ticks;
3. `<tile_id>_context.png` — 3 km around the tile, the tile outlined;
4. `legend_haradskartan_p4.png` — the only legend page the source package supplies;
5. `legend_roads_boundaries.png` — the road and boundary entries (`Riksgräns` … `Häck`) of
   Lantmäteriet's model legend for the economic maps at 1:20,000 (`lm_legend_harad_model`),
   rendered from the registered PDF at 400 dpi;
6. `t06_heldout_r2c2_bare.png` — the worked example: a neighbouring tile of the same sheet,
   bare, as rendered for the earlier blind reading;
7. `example_t06_annotated.png` — the same tile with the owner's frozen reference drawn on
   it (roads by legend class, not-road lines by kind, the unreadable area hatched), key
   below the face.

Nothing else is attached. No modern map, no OpenStreetMap, no other model's answer, no
reference tracing of this tile, and no text from this pilot beyond the caption quoted in
the prompt. Replace `<...>` before sending.

---

You are reading one tile of a Swedish *Häradsekonomiska kartan* sheet, surveyed in the
1890s. Report the **roads and tracks that are drawn on this tile**, as polylines, in the
tile's image coordinate system.

**Coordinates.** Use the map face only: `u` runs right from 0 to 1000, `v` runs down from
0 to 1000, and `(0, 0)` is the top-left outer corner of the face. The gridded image shows
the same face with its margin ticks labelled in these image pixels; the dashed grid is
every 100 image pixels (about 100 m on the ground). Give coordinates as numbers, fractions
allowed, never as descriptions ("north of the church" is not an answer). Do not report
anything outside 0…1000 in either axis.

**What to report.** Every drawn line you judge to be a road, street, farm track or path,
whatever its importance, including ones that only clip a corner of the tile. Put a
waypoint wherever the line changes direction and roughly every 20–60 px along a curve.
Where a road passes under lettering or another symbol, carry it through only if the
drawing on both sides makes that certain; otherwise stop the feature, flag the endpoint,
and say so in the uncertainty note.

**Junctions.** Report each place where three or more road branches meet, with its image
coordinates and branch count, and list the junction's id on every feature that meets it.
Two collinear pieces of the same road are not a junction.

**Classes.** The source package's legend page covers land cover, not roads: it shows only
`Park med parkvägar` (paths inside park symbology) and `Alléteckning` (a road flanked by
tree symbols). Every other class in the list below describes what the *symbol looks like*,
not an official road class. Choose the one that matches what you see, and choose
`unknown` whenever the symbol does not clearly match one — `unknown` is a normal answer,
not a failure:

`double_line_wide`, `double_line_narrow`, `single_line`, `dashed_line`, `avenue_lined`,
`park_path`, `unknown`.

**Road and boundary symbols.** The image after the legend page is a crop of Lantmäteriet's
model legend for the economic maps (*Modell för ekonomiska kartor i skalan 1:20,000*). It
shows the line symbols for boundaries (`Riksgräns` to `Skifteslinje`), railways, roads
(`Landsväg och Bygdeväg`, `Bättre körväg`, `Sämre körväg`, `Gångstig`, `Vinterväg`) and
hedges (`Häck`). Use it to tell roads from boundaries and other lines. It is a model sheet,
and the drawing on this tile may differ from it in detail. Your classes stay the
symbol-form list above.

**Worked example.** The last two images show one person's reading of a neighbouring tile of
the same sheet. Use it as guidance on how this sheet draws roads and other lines, not as a
template for where roads run on this tile. Caption: “The two example images show a
neighbouring 1 km tile of the same sheet, first bare and then with one person's reading of
it drawn on top; the map face is the top 1000 x 1000 px of the second image and the key is
below it. The reading was made with the series legend at hand. Inside the tile it records
these roads, drawn as thick translucent lines: Sämre körväg: 2 lines, 976 m (magenta);
Gångstig: 5 lines, 913 m (green). Every road line is marked certain. It records these lines
as not roads, drawn dashed: Gräns (boundary): 15 lines, 4 588 m (blue, dashed); Bäck eller
dike (stream or ditch): 3 lines, 1 893 m (light blue, dashed); Annan linje (other line): 2
lines, 1 001 m (black, dashed). 1 area is marked unreadable and hatched (resolution too low
for some lines; 3 % of the tile). Outside the hatched area, a line on the map that carries
no road colour is not a road in this reading.”

**Evidence.** For every feature and every junction, write one short note saying what is
visible at that position — how many lines, their spacing, colour and weight, whether they
are cased, what symbols sit alongside. Describe the drawing, not the landscape, and never
infer from anything you know about modern Sweden.

**Ambiguity is an output, not a problem to solve.** When the drawing supports more than
one reading — a different class, a different course, a field or property boundary rather
than a road, one feature or two — record the alternatives in `alternatives` and keep the
uncertainty note. Do not pick a reading in order to look decisive, and do not leave a
doubtful feature out: report it with its doubt attached.

**Do not** guess where a road "must" go, complete a network, smooth a course into a
plausible shape, or use any knowledge of the modern road network.

**Answer** with a single JSON object with this shape: {"tile_id": "<tile_id>", "features": [{"feature_id": "f01", "waypoints_uv": [[u, v], ...], "proposed_class": "<one of the classes above>", "junction_ids": ["j01", ...], "endpoint_flags": ["edge_of_tile" | "dead_end" | "junction" | "obscured", <same for the end>], "evidence_note": "...", "uncertainty_note": "...", "alternatives": [{"proposed_class": "...", "note": "..."}]}], "junctions": [{"junction_id": "j01", "uv": [u, v], "branch_count": 3, "evidence_note": "..."}], "unreadable_areas": [{"bbox_uv": [u0, v0, u1, v1], "why": "..."}], "overall_note": "at most three sentences; what was hard"}. If you can read nothing on this tile, return the object with empty features and junctions and say why in overall_note.
