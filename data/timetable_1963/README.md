# The 1963 timetable, as transcribed

Tables 161 ("Göteborg–Skara–Forshem", pp. 194–195) and 161 a ("Göteborg–Sjövik–Göteborg", p. 196)
of *Sveriges Kommunikationer* 1963 nr 6 (SJ, valid from 26 May 1963), from scans hosted by
Timetable World (<https://timetableworld.com/>). Credited to SJ 1963 and Timetable World; removed on
request under the site's takedown policy. The scans themselves are not included.

**How it was read:** an AI agent read the scans directly at full resolution, cell by cell (no OCR),
in about 0.6 hours.

- **Table 161:** every train column of the corridor stretch Göteborg C – Nossebro, both directions.
  Beyond Nossebro, only station names and km posts.
- **Table 161 a:** the station list and the two terminal times of each train.

**Checks:**

- 368 cells were transcribed: none unreadable, 7 of medium confidence (read twice, or a symbol not
  in the legend).
- Three whole trips were read a second time, cell by cell.
- Where the two tables repeat the same trains, their times agree to the minute.

**Files:**

| File | What |
|---|---|
| `service_stops.parquet` | one row per transcribed cell: station as printed, time, stop and day symbols, footnotes |
| `service_trips.parquet` | one row per printed train column, with train number and operating days |
| `timetable_stations.parquet` | station lines with km posts as printed |
| `legend_161.json` | the timetable's symbols and footnotes as read |

Identifiers of the form `RQ-S7-…` refer to the author's review queue (uncertain readings held for
a decision), which is not part of this repository. The VGJ running times used in the maps
(`data/vgj_1963/`) are derived from this transcription.
