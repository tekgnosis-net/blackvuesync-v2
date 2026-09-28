# BlackVue recording file formats

A durable reference for the file-naming convention and on-disk content of BlackVue
dashcam recordings synchronized by this tool. The binary/text formats below were
**verified against a real DR-series sample** (June 2026); older community tools
(`bartbroere/blackvue-acc`, `gandy92/blackclue`) decoded the same telemetry when it
was embedded inside the MP4, whereas current firmware writes separate sidecar files
-- the inner formats align.

## Filename convention

```text
YYYYMMDD_HHMMSS_<type><direction>[upload].<ext>
```

| Part | Meaning |
| --- | --- |
| `YYYYMMDD_HHMMSS` | local timestamp (camera clock / timezone) |
| `<type>` | one recording-type letter (table below) |
| `<direction>` | camera direction letter (`F`/`R`); video + thumbnail carry it, the `.3gf`/`.gps` omit it |
| `[upload]` | optional `L` (live) / `S` (substream) flag |
| `<ext>` | `mp4` \| `thm` \| `3gf` \| `gps` |

The filename regex lives in `blackvuesync_v2/sync.py` (`filename_re`) and is parsed into a
`Recording` dataclass by `to_recording()`.

### Per-recording-instant file set

All artifacts of one recording instant share the base `YYYYMMDD_HHMMSS_<type>`:

| File | Content | Per |
| --- | --- | --- |
| `<base><dir>.mp4` | H.264 video | camera direction (e.g. `_PF.mp4` front, `_PR.mp4` rear) |
| `<base><dir>.thm` | JPEG thumbnail | camera direction |
| `<base>.3gf` | G-sensor / accelerometer (binary) | recording (no direction) |
| `<base>.gps` | GPS track (NMEA text) | recording (no direction) |

So front and rear videos pair by sharing base+type and differing only by the
direction letter; they share a single `.gps` and `.3gf`.

### Upload-flag filenames

A video listed with an upload flag (e.g. `20260607_101500_NFL.mp4`) is stored under
that exact name, but `sync.py` stores its thumbnail and sidecars **without** the flag
(`20260607_101500_NF.thm`, `20260607_101500_N.gps`, `20260607_101500_N.3gf`). The
viewer index (`blackvuesync_v2/server/viewer_index.py`) therefore keeps the real
on-disk `.mp4` name per direction and builds video URLs from it; it looks up the
thumbnail under the unflagged name first, then the flagged one. When both a flagged
and an unflagged video exist for one direction, the unflagged one is used.

## Recording-type codes

| Code | Meaning |
| --- | --- |
| `N` | Normal (continuous while driving) |
| `E` | Event (impact, sudden braking, or swerving) |
| `P` | Parking (motion or impact while parked) |
| `M` | Manual (button press / proximity-sensor tap) |
| `R` | Manual backup (clips saved while reviewing) |
| `T` | Timelapse |

`blackvuesync_v2/sync.py` recognizes a broader set across models
(`NEPMIOATBRXGDLYF`: e.g. `I` impact, `O` overspeed, `A` acceleration, `B` braking,
`R`/`X`/`G` geofence, `D`/`L`/`Y`/`F` DMS). The viewer displays the letter plus a
best-effort label.

> Note the collision: `R` is both a recording **type** (manual backup) and a camera
> **direction** (rear). Position in the filename disambiguates -- the type letter
> immediately follows the timestamp; the direction letter (if any) follows the type.

## Camera-direction codes

| Code | Meaning |
| --- | --- |
| `F` | front camera |
| `R` | rear camera (or interior on some 3-channel models) |
| `I` / `O` | interior / optional (recognized by `sync.py`; uncommon) |

## `.gps` -- GPS track (NMEA-0183 text)

- Plain text. Each NMEA sentence is **prefixed by a wall-clock timestamp in
  `[milliseconds-since-epoch]`** and terminated by CRLF; blank lines separate entries.
- The talker is **multi-GNSS `$GN...`** (GPS+GLONASS+Galileo), **not** `$GP...`.
  Parse **by sentence type, talker-agnostic** (`$G?RMC` / `$G?GGA`).
- Sentences observed: `$G?RMC` (position, speed-over-ground in **knots**, date) and
  `$G?GGA` (position, fix quality, satellite count, altitude).
- Coordinates are `DDMM.mmmmm` + hemisphere (`N`/`S`/`E`/`W`) -> decimal degrees:
  `degrees + minutes/60`, negated for `S`/`W`.
- Example (anonymized real framing):

  ```text
  [1780855916491]$GNRMC,HHMMSS.00,A,DDMM.mmmmm,S,DDDMM.mmmmm,E,0.000,,DDMMYY,,,A,V*06
  [1780855916491]$GNGGA,HHMMSS.00,DDMM.mmmmm,S,DDDMM.mmmmm,E,1,12,0.68,52.8,M,19.4,M,,*6B
  ```

- A stationary/parked recording may contain a single fix (speed `0.000`); some
  recordings have no `.gps` file, or a `.gps` with no valid fix.

## `.3gf` -- G-sensor / accelerometer (binary)

- **Big-endian, fixed 10-byte records, packed back-to-back, no header.** File size is
  always a multiple of 10 (`size / 10` = sample count).
- Record layout (`struct` format `>Ihhh`):

  | Offset | Type | Field |
  | --- | --- | --- |
  | 0 | `uint32` (BE) | milliseconds from recording start |
  | 4 | `int16` (BE) | X axis (raw) |
  | 6 | `int16` (BE) | Y axis (raw) |
  | 8 | `int16` (BE) | Z axis (raw) |

- Sample rate **≈ 10 Hz** (observed ~105 ms between samples; a ~60 s recording yields
  ~560 samples).
- Axis values are raw `int16`. **Scale: `raw / 128 = g`.** Confirmed by two
  independent sources: a real DR-series parking recording (562 samples, mean
  vector magnitude 1.025 g when stationary ≈ gravity) and
  `bartbroere/blackvue-acc` (`blackvue_acc.py`: `/ 128  # 1G is assumed to be
  128 as integer`). The implementation lives in
  `blackvuesync_v2/server/gsensor.py` (`SCALE_G = 128.0`).

## `.thm` -- thumbnail

Baseline JPEG (JFIF), 704×480 in the sample. Served to the browser as `image/jpeg`
(the `.thm` extension is not in the standard MIME map).

## `.mp4` -- video

H.264 in an MP4 (`ftyp mp42`) container -- browser-native, **no transcoding needed**.
Front and rear are separate files sharing base+type, differing by direction.

## Time alignment (for the viewer)

`.gps` timestamps are **absolute epoch-ms**; `.3gf` timestamps are **ms-from-start**.
Both reduce to "elapsed seconds from recording start", which maps directly to HTML5
`video.currentTime`:

- **GPS t=0** is the earliest timestamp of *any* recognized `RMC`/`GGA` sentence in
  the file, with or without a fix. Sentences written before the receiver has a fix
  still mark the start of the recording, so the first fixed point keeps its real
  offset into the video instead of being pulled to 0. Non-finite values (`nan`,
  `inf`) are dropped: a non-finite speed becomes `null`, a non-finite coordinate
  skips the point.
- **G-sensor t** is the record's ms-from-start divided by 1000.

Each telemetry point is kept with the index of its segment and its segment-local
time. The map marker and speed readout use only points of the segment that is
playing, picking the one nearest to `video.currentTime`; points of other segments
are never matched, so the marker cannot jump into a neighbouring segment.

Across an auto-advanced journey (and in `full` journey mode, where the whole
chain's telemetry is prefetched), segments are laid end to end on one session
timeline to order the accumulated map path and G-sensor chart. A segment's length
on that timeline is its video duration (from the video's `loadedmetadata`), falling
back to its telemetry span when the video has not been loaded yet, then to 60 s.

## References

- [`bartbroere/blackvue-acc`](https://github.com/bartbroere/blackvue-acc) -- `.3gf`
  accelerometer extraction.
- [`gandy92/blackclue`](https://github.com/gandy92/blackclue) -- GPS and acceleration
  extraction from BlackVue MP4s.
