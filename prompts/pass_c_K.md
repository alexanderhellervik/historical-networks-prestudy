# Pass C, arm K — cross-review in a fresh context, with the legend crop and worked example

Informed-reading experiment (`experiments/informed_reading/plan.md`). Sent once per
(tile, reviewing model family) in a **fresh context**: each family reviews the other
family's arm-K reading, never its own. The text below the line is the t06 pass-C prompt
(`experiments/vision_roads/prompts/pass_c_review.md`) with the same two paragraphs as
`pass_a_K.md` added after **Classes**. Attachments, in this order:

1. `<tile_id>_bare.png` — the map face alone;
2. `<tile_id>_grid.png` — the same face with the 100-pixel grid;
3. `<tile_id>_proposal_ids.png` — the same face with only feature ids at the line midpoints
   (the other family's arm-K reading);
4. `<tile_id>_proposal_overlay.png` — the same face with the candidate polylines drawn as
   thin coloured lines with their ids;
5. `legend_haradskartan_p4.png`;
6. `legend_roads_boundaries.png` — the road and boundary entries (`Riksgräns` … `Häck`) of
   Lantmäteriet's model legend for the economic maps at 1:20,000 (`lm_legend_harad_model`),
   rendered from the registered PDF at 400 dpi;
7. `t06_heldout_r2c2_bare.png` — the worked example: a neighbouring tile of the same sheet,
   bare, as rendered for the earlier blind reading;
8. `example_t06_annotated.png` — the same tile with the owner's frozen reference drawn on
   it (roads by legend class, not-road lines by kind, the unreadable area hatched), key
   below the face.

The overlay is **not** labelled with who drew it, and no model output other than the
overlay, no OpenStreetMap layer, no reference tracing of this tile and no pilot text
beyond the caption quoted in the prompt is attached. Replace `<...>` before sending.

---

You are reading one tile of a Swedish *Häradsekonomiska kartan* sheet, surveyed in the
1890s.

**First, read the bare tile.** Before looking at the overlay, work out for yourself where
the drawn roads, tracks and paths are. Then look at the overlay: it shows one other
reading of the same tile, drawn by someone whose identity you do not know. It is a
proposal, not an answer key, and it may be wrong, incomplete or invented.

**Coordinates.** As on the tile: `u` right 0…1000, `v` down 0…1000, `(0, 0)` at the
top-left outer corner of the map face; the dashed grid is every 100 image pixels.

**Report, for every overlay feature id shown**, one review entry: whether the drawn
polyline follows a road that is really drawn there (`agree`), follows it but with the
wrong class (`agree_with_different_class`), follows roughly the right road on a wrong
course — then give your own waypoints (`partly_agree_course_differs`), does not correspond
to a drawn road at all (`disagree_not_a_road`), or cannot be judged from what is visible
(`cannot_tell`). Give a short note saying what is visible at that position that decides
it. `cannot_tell` is a proper answer; do not agree by default.

**Then report what the overlay misses**: roads drawn on the tile that it does not show, in
the same feature shape as the overlay uses (waypoints, class from the vocabulary or
`unknown`, endpoint flags, evidence note, uncertainty note, alternatives).

**Then report contested junctions**: places where the number of branches, which lines
meet, the position, or the existence of a junction is genuinely in doubt.

**Classes** are the same vocabulary: `double_line_wide`, `double_line_narrow`,
`single_line`, `dashed_line`, `avenue_lined`, `park_path`, `unknown`.

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

**Do not** adjust your reading to agree with the overlay, and do not disagree in order to
look independent. Where the drawing genuinely does not decide, say so and record the
alternatives; a disagreement recorded honestly is the output of this pass.

**Answer** with a single JSON object and nothing else, valid against
`prompts/pass_c_schema.json`, with `tile_id` set to `<tile_id>`.
