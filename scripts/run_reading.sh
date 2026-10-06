#!/usr/bin/env bash
# One map-tile reading by Codex (gpt-6.1-sol), arm K: the tile's bare, gridded and context views,
# the legend page, the legend crop of road and boundary symbols, and the worked example (a
# neighbouring tile, bare and with a person's reading drawn on it). One fresh non-interactive call
# per tile; web search, connected apps, plugins and browsing disabled; sandbox read-only; the
# working directory is empty. Refuses to overwrite an existing answer.
#
# Usage: TILES_DIR=work/corridor_1890s/tiles OUT_ROOT=work/corridor_1890s \
#        bash scripts/run_reading.sh K <tile_id> [r1]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ARM=${1:?usage: run_reading.sh K <tile_id> [replicate]}
T=${2:?usage: run_reading.sh K <tile_id> [replicate]}
REP=${3:-r1}
MODEL=${MODEL:-gpt-6.1-sol}
FAMILY=${FAMILY:-sol61}
TD=${TILES_DIR:?set TILES_DIR}
O=${OUT_ROOT:?set OUT_ROOT}
EX=$ROOT/data/worked_example   # shipped with the repository (see LICENSES.md)
P=$ROOT/prompts/pass_a_K_inline.md
SFX=""; [ "$REP" != r1 ] && SFX="_${REP}"
IMGS=("$TD/${T}_bare.png" "$TD/${T}_grid.png" "$TD/${T}_context.png" "$EX/legend_haradskartan_p4.png"
  "$EX/legend_roads_boundaries.png" "$EX/example_bare.png" "$EX/example_annotated.png")
for f in "${IMGS[@]}"; do [ -f "$f" ] || { echo "missing attachment $f" >&2; exit 1; }; done
OUT=$O/model_outputs/${T}_${FAMILY}_${ARM}${SFX}_passA.json
[ -e "$OUT" ] && { echo "$OUT exists; refusing to overwrite" >&2; exit 1; }
mkdir -p "$O/model_outputs" "$O/runs" "$O/runs/codex_root"
BODY=$(awk 'f{print} /^---$/{f=1}' "$P")
PROMPT=${BODY//<tile_id>/$T}
IARGS=(); for f in "${IMGS[@]}"; do IARGS+=(-i "$f"); done
EV=$O/runs/${T}_${FAMILY}_${ARM}${SFX}_passA.events.jsonl
START=$(date -u +%FT%TZ); S0=$(date +%s)
timeout 1800 codex exec -m "$MODEL" -s read-only --skip-git-repo-check -C "$O/runs/codex_root" --json \
  -c 'web_search="disabled"' --disable apps --disable plugins --disable browser_use --disable computer_use \
  --disable in_app_browser --disable remote_plugin \
  "${IARGS[@]}" -o "$OUT" "$PROMPT" < /dev/null > "$EV" 2> "$O/runs/${T}_${FAMILY}_${ARM}${SFX}_passA.err"
RC=$?
python3 - "$T" "$ARM" "$REP" "$FAMILY" "$MODEL" "$RC" "$START" "$(( $(date +%s) - S0 ))" "$EV" "$O" <<'PY'
import json, pathlib, sys
t, arm, rep, family, model, rc, start, secs, ev, o = sys.argv[1:]
usage = None
for line in pathlib.Path(ev).read_text().splitlines():
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("type") == "turn.completed":
        usage = e.get("usage")
rec = {"tile_id": t, "arm": arm, "replicate": rep, "family": family, "model": model,
       "started": start, "wall_s": int(secs), "exit_code": int(rc), "usage": usage}
with (pathlib.Path(o) / "runs" / "pass_a_log.jsonl").open("a") as fh:
    fh.write(json.dumps(rec) + "\n")
PY
