# Sub-Project #7 -- Camera Settings (read/write) -- Design Spec

**Date:** 2026-10-05
**Repo:** tekgnosis-net/blackvuesync-v2
**Status:** Design approved section by section in chat; this spec awaits review.
The implementation plan follows only after this spec is approved.
**Series:** the deferred seventh sub-project. The original deferral is recorded in
`2026-05-19-sub-project-2-dashboard-design.md` ("Amendment 2026-05-20").

---

## 1. Goal

Every setting in the dashcam's `Config/config.ini` can be viewed and changed from
the app, grouped the way the BlackVue app groups them, so the phone app is only
needed to recover from a bad Wi-Fi change.

### Success criteria

- At home, any camera setting can be changed in the browser and the app confirms
  the change by reading the camera's file back.
- Wi-Fi passwords behave like passwords: masked, revealed one at a time with an
  eye icon, never logged, never in API responses unless explicitly requested.
- A change that formats the camera's microSD card cannot be applied without an
  explicit warning that names how many recordings would be lost.
- No byte of the camera's file changes except the values the user edited.

## 2. Decisions taken in the design conversation

| Topic | Decision |
| --- | --- |
| Scope | All settings read/write, including the camera hotspot and the three home Wi-Fi hotspots. Worst case accepted: recovery with the phone app in the car. |
| Placement | A **Camera** group in the existing Settings sidebar, under the app's own sections. |
| Tabs | **Basic** (`[Tab1]`), **Sensitivity** (`[Tab2]`), **System** (`[Tab3]`), **Wi-Fi** (`[Wifi]`), **Cloud** (`[Cloud]`), plus **Backups**. The names match the BlackVue app ("Basic settings", "Sensitivity settings", "System settings", "Wi-Fi settings", "Cloud settings"). |
| Offline | Show the last-known settings read-only, with when they were read. Editing unlocks when the camera is reachable. |
| Key metadata | A Python descriptor table, `camera_schema.py`, in the style of `settings_form.py`. Unknown keys stay editable under "Other". |
| Save model | Edits are staged in the browser across tabs; one **Review & apply** uploads the whole file once. |
| Restart button | Added later, only if the undocumented port-9771 restart works on the DR900X Plus (section 10). |

## 3. Verified facts

**The camera** (read 2026-10-04 from the camera on Kumar's LAN, through the NAS):
DR900X Plus, firmware 1.015, config version 1.071, revision 2230. `config.ini`
has 85 keys in five sections: `[Tab1]` 19, `[Tab2]` 9, `[Tab3]` 45, `[Wifi]` 4,
`[Cloud]` 8 (full list in Appendix A). It holds the camera hotspot password
(`ap_pw`) and three home Wi-Fi passwords (`sta_pw`, `sta2_pw`, `sta3_pw`).

**Password encoding (correction, 2026-10-05).** An earlier draft said these were
plain text; that was assumed from the key names, never checked. On the DR900S,
`*_pw` values are 64 hex characters: the password zero-padded to 32 bytes and
encrypted with AES-128-CBC under a fixed key and IV built into the BlackVue app.
The key and IV were published in the `eyJhb/blackvue-cve-2023` research
(`software/wifi-decrypt/wifi-decrypt.py`), so the values are encrypted but not
secret from anyone who reads that research. **Verified on the DR900X Plus (2026-10-05):** all four
values in Kumar's file are 64 hex characters, and each decrypts to printable
text followed by exact zero padding, and Kumar confirmed the decrypted
lengths match his passwords. A wrong key would yield random bytes. So:

- **Reveal:** decrypt the stored value.
- **Write:** encrypt the new password the same way, as uppercase hex.
- **New dependency:** the standard library has no AES, so this adds
  `cryptography` as a runtime dependency of the server package (not of
  `sync.py`). It is checked for musl wheels on amd64 and arm64 when the
  dependency is added.
- **Fallback:** a value that is not 64 hex characters is treated as plain text
  in both directions, so a firmware that stores plain text still works.

**HTTP endpoints** (no authentication on the LAN): `GET /Config/config.ini`,
`GET /Config/version.bin`, `GET /blackvue_vod.cgi`, `GET /Record/<file>`,
`blackvue_live.cgi`, and `upload.cgi`, which accepts "a replacement" config file
and is also the firmware-upload endpoint. Its request format (method, multipart
field name, file name) is **not documented anywhere** we found; section 10 finds
it on the real camera.

**Formatting (critical).** The DR900X Plus manual, both the computer and
smartphone pages, says verbatim:

> Please backup necessary recordings before changing time or image quality
> settings. If any of the aforementioned settings are changed and saved, the
> dashcam will format the microSD card and delete all recordings stored on the
> card including locked event files in order to ensure optimal performance.

This most likely explains the reboot observed after time/DST changes. It turns those keys
from "reboots the camera" into "**erases the camera's recordings**", and drives
the format guard in section 7. This is new since the chat design (which treated
them as reboot-only) and is the main thing to review in this spec.

**Current exposure.** `/api/dashcam/info` returns the whole parsed config,
including all four passwords, to any signed-in user (anyone, in auth mode
`none`). The dashboard card only renders the first 8 `[Tab1]` entries, so the
passwords are in the JSON but not on screen. Phase A fixes this.

**Sources:** BlackVue DR900X Plus manual, "Changing settings using your computer"
and "... using your smartphone"; `johnhamelink/blackvue` wiki (`upload.cgi` for
config and firmware); `Digital-Nebula/hackvue` endpoint list;
`eyJhb/blackvue-cve-2023` (port 9771 service, DR750 LTE).

## 4. Architecture

### 4.1 Modules (all under `blackvuesync_v2/server/`)

- **`camera_config.py`** -- the camera I/O. Standard library only.
  - `fetch(address, timeout) -> CameraFile | None`: reads `config.ini` (raw
    bytes) and `version.bin`.
  - `CameraFile`: the raw bytes plus an index of `(section, key) -> line span`.
    Parsing never re-serialises; it only locates lines.
  - `patch(raw: bytes, changes: dict[(section, key), str]) -> bytes`: replaces
    the value part of exactly the changed `key=value` lines. Line endings,
    order, spacing, unknown keys and every other byte stay identical. Raises on
    a missing or duplicated key.
  - `self_check(before, after, changes)`: re-parses both and asserts that only
    the changed keys differ, and that they hold the new values.
  - `decrypt_password(value) -> str` / `encrypt_password(text) -> str`: the
    BlackVue scheme from section 3, with the plain-text fallback. These sit in a
    separate `camera_crypto.py`, the only module that imports `cryptography`.
  - `upload(address, raw, timeout)`: posts to `upload.cgi` in the format found
    in section 10.
  - `wait_until_back(address, timeout, poll)`: polls `config.ini` until it
    answers or the timeout passes.
  - Snapshot and backup helpers (section 4.3).
- **`camera_schema.py`** -- the descriptor table.
  - `CameraField(section, key, label, widget, options, unit, scale, min, max,
    secret, formats_card, read_only, help)`.
  - `widget` is one of `select`, `toggle`, `number`, `text`, `password`,
    `time`.
  - `options` maps raw values to labels (`"0" -> "Highest (Extreme)"`).
  - `scale` converts display units (`LowvoltageVolt` `1190` shows as 11.9 V).
  - `TABS`: Basic -> `Tab1`, Sensitivity -> `Tab2`, System -> `Tab3`, Wi-Fi ->
    `Wifi`, Cloud -> `Cloud`.
  - `validate(field, value) -> list[str]` (section 7.1). Keys present in the
    file but absent from the table get a generic `text` descriptor in an
    "Other" group on their tab.
- **`routes/api_camera.py`** -- the JSON API (section 4.2).
- **`routes/api_dashcam.py`** -- switches to `camera_config.fetch()` and masks
  secret values (`***`), so the dashboard card and `/api/dashcam/info` never
  carry a password.

### 4.2 Endpoints

All `@login_required`; `POST` routes are CSRF-protected globally by Flask-WTF.

| Method | Path | Phase | Behaviour |
| --- | --- | --- | --- |
| `GET` | `/api/camera/config` | A | Tabs with fields and values, `online`, `read_at`, `model`, `firmware`. Secret values are `"***"` with `"has_value": true/false`. Served from a live read when the camera answers, else from the snapshot with `online: false`. |
| `GET` | `/api/camera/secret?key=Cloud.sta_pw` | A | `{"value": "..."}` for one key whose descriptor is `secret`; 404 for any other key. Live read, else snapshot. |
| `POST` | `/api/camera/refresh` | A | Re-reads the camera now; 200 with the same body as `GET` plus `changed`: `[{"key", "from", "to"}]` against the previous snapshot (secret values reported as `"changed"`), or 409 `CAMERA_OFFLINE`. |
| `GET` | `/api/camera/pending` | C | `{"pending": n}`: recordings listed by the camera with no downloaded file at their destination path. Used by the format guard. |
| `POST` | `/api/camera/config` | C | Apply (section 5). Body `{"changes": {"Tab3.VOLUME": "4", ...}, "confirm_format": bool, "accept_loss": bool}`. |
| `GET` | `/api/camera/backups` | C | `[{"name", "created", "size"}]`, newest first. |
| `POST` | `/api/camera/backups/<name>/restore` | C | Uploads that backup through the same apply pipeline, with the same guards. |

### 4.3 Files

Under `<config dir>/camera/`, next to `settings.json`. The directory is `0700` and
the files `0600`, since they hold Wi-Fi passwords; loading a file with wider
permissions logs a warning and tightens it.

- `config.ini` -- last successful read (snapshot), raw bytes.
- `snapshot.json` -- `read_at` (UTC), `model`, `firmware`.
- `backups/config-YYYYMMDD-HHMMSS.ini` -- the camera's file as read just before
  each upload. The name uses local time; the UI shows local time.

### 4.4 App settings

A new `camera` section, tier `immediate`, added in phase C (when it is first
used). Read at run time with clamps:

| Field | Default | Range | Purpose |
| --- | --- | --- | --- |
| `backup_count` | 10 | 1-100 | Backups kept; older ones are pruned. |
| `verify_timeout_seconds` | 120 | 10-600 | How long to wait for the camera after an upload (format and reboot). |
| `verify_poll_seconds` | 5 | 1-30 | Interval between read-back attempts. |

No env seeds, matching the `viewer` section. A missing section loads its defaults,
so no schema version bump.

## 5. Apply flow (phase C)

1. **Validate** every change against the schema (section 7.1). 422 on failure.
2. **Lock**: acquire the `sync_runner` lock without blocking. A running sync or
   apply returns 409 `SYNC_RUNNING` / `APPLY_RUNNING`. A scheduled sync that
   fires during an apply is skipped with a log line.
3. **Fresh read** of `config.ini`. Unreachable returns 409 `CAMERA_OFFLINE`. Any
   changed key missing from the fresh file returns 422.
4. **Format guard** (section 7.2). Returns 409 `FORMAT_CONFIRMATION_REQUIRED`
   when needed.
5. **Backup** the fresh file; prune to `backup_count`.
6. **Patch** only the changed keys, then **self-check**. On mismatch, 500 and
   nothing is uploaded.
7. **Upload**. A transport or HTTP error returns 502 `UPLOAD_FAILED`; the backup
   remains.
8. **Wait and verify**: poll until the camera answers or `verify_timeout_seconds`
   passes, then compare the changed keys.
9. **Result** (200): `verified` (bool), `kept` (keys whose camera value differs
   from what was sent), `backup`, `came_back` (bool). The snapshot is updated
   from the camera's read-back. The camera is the source of truth.
10. **Log** one INFO line naming the changed keys; passwords appear as
    `(changed)`.

## 6. User interface

- **Sidebar.** Headings **App** (existing sections) and **Camera** (Basic,
  Sensitivity, System, Wi-Fi, Cloud, Backups). Camera items use
  `data-section-nav`, so the contrast audit covers them.
- **Pane header.** `Camera > Basic`, then `DR900X Plus · fw 1.015 · ● online` and
  a **Refresh** button.
- **Controls per schema widget.**
  - Dropdowns for coded values, toggles for 0/1 keys.
  - Number fields with unit, limits and scale.
  - A time picker for the reboot hour.
  - Text fields for SSIDs and `userString` (with the manual's 20-character
    rule).
  - Password fields with an eye button (`aria-label` "Show password" / "Hide
    password", `aria-pressed`).
- **Password semantics.** A masked field shows `••••••••` when the key has a value,
  else empty. The eye fetches `/api/camera/secret` for that key only. An
  untouched password is never sent, so it is never changed.
- **Badges.** Format-triggering keys show **⚠ erases camera recordings**.
- **Other.** Unknown keys appear under "Other" on their tab as text fields
  labelled with the raw key.
- **Staging (phase C).** Changes live in the browser across tabs. Changed fields and
  their sidebar item get a dot. A sticky bar shows the count, **Discard** and
  **Review & apply**. `beforeunload` warns about unapplied changes.
- **Review dialog.**
  - A *Setting / From / To* table; passwords show "changed".
  - Warnings:
    - Format keys (section 7.2), in red, with the pending count.
    - Wi-Fi changes: "if wrong, the camera may not rejoin your network; fix it
      with the phone app".
- **Apply progress.** Reading camera → Backing up → Uploading → Waiting for camera
  → Verifying, then the result.
- **Offline.** Fields are disabled under a banner: "Camera offline: showing settings
  read at 08:12. Editing unlocks when the camera is reachable." No apply bar.
- **Changed on the camera.** After **Refresh**, any keys that differ from the
  previous snapshot are listed ("3 settings changed on the camera: Time zone
  1000 → 930, ..."); passwords show "changed".
- **Phase A** ships the panes read-only, with the note "Editing arrives in a later
  release". Reveal works.
- **Dashboard card.** Unchanged layout, secrets masked, plus an "Edit camera
  settings" link.
- **Constraints.** CSP-clean: no inline scripts, and Alpine CSP-build rules
  (directives are bare property or method references). Colours come from the
  tokens.

## 7. Safety

### 7.1 Validation

- `select` values must be in `options`. `number` values must be integers within
  `min`/`max` after `scale`.
- Every value is refused if it contains a control character (CR, LF, NUL and
  others), which blocks line injection such as `\n[Cloud]\nsta_pw=...`.
- SSIDs: at most 32 bytes as UTF-8. Passwords: 8-32 characters (the scheme pads
  to 32 bytes; longer values are refused until a test shows the camera accepts
  them); home-network
  passwords may also be empty, which clears that network.
- `read_only` keys (`CloudSettingVersion`, `RecordTime`) are refused.
- A key that appears twice in one section is refused as ambiguous.

### 7.2 Format guard

**Keys.** `TimeSet`, `SetTime`, `TimeZone`, `Daylight`, `GpsSync` (the manual's
"time settings") and `VideoQuality` ("image quality"). `ImageSetting` is treated
the same until calibration (section 10) shows otherwise.

**Before the dialog opens**, the UI calls `/api/camera/pending`. The dialog then
says, in red: "The camera will format its microSD card and delete **all**
recordings on it, including locked events. *N* recordings have not been downloaded
yet." It offers **Sync now first**, and **Apply anyway** only after a checkbox:
"I accept losing *N* recordings".

**On the server**, a format key requires `confirm_format: true`. The server
recomputes the pending count, and when it is non-zero, also requires
`accept_loss: true`. Otherwise it returns 409 `FORMAT_CONFIRMATION_REQUIRED`
with the count.

**Pending count.** `get_dashcam_filenames()` lists the camera; each `.mp4` is
mapped through `to_recording()` and `get_filepath()` (honouring grouping and the
include/exclude filters); a file counts as pending when no file exists at that
path.

### 7.3 Failure reporting

| Situation | Response |
| --- | --- |
| Camera offline at the fresh read | 409 `CAMERA_OFFLINE`; nothing changed. |
| Sync or apply running | 409 `SYNC_RUNNING` / `APPLY_RUNNING`. |
| Format guard not satisfied | 409 `FORMAT_CONFIRMATION_REQUIRED`, with `pending`. |
| Patch self-check fails | 500 `PATCH_CHECK_FAILED`; nothing uploaded. |
| Upload error | 502 `UPLOAD_FAILED`; backup kept; UI suggests Refresh. |
| Camera not back in time | 200 with `came_back: false, verified: false`. The UI adds "check with the phone app" when Wi-Fi keys changed. The snapshot is not updated. |
| Camera kept other values | 200 with `kept` listing the keys. The snapshot takes the camera's values. |

### 7.4 Secrets

- Secret keys: `ap_pw`, `sta_pw`, `sta2_pw`, `sta3_pw`. `userString` is the
  manual's "User text overlay", not a secret.
- On the camera they are stored encrypted (section 3). `/api/camera/secret`
  returns the decrypted text; apply encrypts new values before patching.
- Values are never logged and never returned except by `/api/camera/secret`.
- The docs note that in auth mode `none` anyone on the LAN can reveal them. That
  is no wider than the camera, which serves `config.ini` to the LAN without a
  password.

## 8. Testing

- **Fixture:** `test/fixtures/camera/dr900x-plus-config.ini`, Kumar's file
  with every personal value replaced and invented passwords encrypted with the
  camera's scheme (plaintexts in the fixture's README). Never a real file.
- **`camera_config` units:**
  - Patch byte preservation on CRLF and LF files, with a non-ASCII SSID.
  - Missing, duplicate and control-character refusals.
  - Self-check catches a tampered patch.
  - Snapshot and backup permissions and pruning.
- **Fake camera:** a stdlib `http.server` fixture serving `config.ini` and
  accepting `upload.cgi`, with modes "reboots for N s" and "never comes back".
  Covers fetch, upload, verify and timeout.
- **`camera_schema` units:**
  - An Appendix A key-name fixture, with dummy values and no real secrets: every
    key is described or lands in "Other".
  - Options, limits and scale; the secret and format flags.
- **Routes:**
  - Masking in `/api/camera/config`, `/api/dashcam/info` and the dashboard card
    HTML; `/secret` returns one key and 404s others.
  - The 409 / 422 / 500 / 502 paths, the format guard with and without pending
    recordings, the success path with a backup and verify, and restore.
  - Sign-in and CSRF are required.
- **Behave:** the mock dashcam serves `config.ini` and accepts `upload.cgi`. One
  scenario changes a setting and asserts the upload differs only on that line.
  Read `features/CLAUDE.md` first.
- **Playwright** (Chromium in CI, WebKit locally in Docker):
  - Staging across two tabs, the review dialog, the format warning and checkbox,
    and the result.
  - The eye toggle and its labels; offline disabled fields.
  - The contrast audit covers the new panes.
- **Screenshots:** the demo dashcam serves a synthetic `config.ini` with invented
  SSIDs and passwords.

## 9. Phases and releases

| Phase | Content | Release |
| --- | --- | --- |
| **A** | Secret masking in `/api/dashcam/info` and the card; `camera_config` read side, snapshot and permissions; `camera_schema`; `camera_crypto` with the `cryptography` dependency (reveal); `GET /api/camera/config`, `/secret`, `POST /refresh`; read-only Camera panes with offline banner, eye reveal and "changed on the camera" after Refresh. | 3.2.0 |
| **B** | Camera tests 1-3 and calibration (section 10). Findings are recorded in `docs/reference/blackvue-camera-config.md`. | none |
| **C** | `upload`, apply pipeline, format guard, `/pending`, backups and restore, `camera` app settings, staging UI, review dialog, Behave scenario. | 3.3.0 |
| **D** | Restart camera (port 9771), only if test 4 passes. | patch or minor |

## 10. Tests on the real camera (each needs Kumar's OK, car parked at home)

The development workstation cannot reach the camera (on 2026-10-04 it timed out
while the NAS read the camera at the same moment), so these run from the NAS or
through the deployed app.

0. **Calibration, no writes from the app** (needs phase A deployed).
   - Kumar changes one setting at a time in the BlackVue phone app, then presses
     **Refresh** in the Camera panes.
   - The "changed on the camera" list shows the key and its old and new value.
     Passwords show only "changed".
   - This pins value encodings (`TimeZone=1000`, `SpeedUnit`, `VideoQuality`,
     `PSENSOR`, the motion-region bitmask), and whether the phone app's save
     reformats anything.
0a. **Password format: done 2026-10-05**, from the file Kumar pasted, printing
   only lengths (section 3); Kumar confirmed the decrypted lengths.
1. **`GET /upload.cgi`** (read-only). It may return the upload form, revealing
   the field name.
2. **No-op upload** of the camera's own unchanged file. Record the HTTP
   response, whether it reboots, and confirm the read-back is byte-identical.
   Done only after a full sync, in case it triggers a format.
3. **One harmless change and back**: `VOLUME` 5 → 4 → 5, verified each way.
4. **Port 9771** (for phase D): TCP connect; then `ping` and `get time`; then one
   `restart`, watching port 80 come back.

If test 1 does not reveal the format, the fallback is the multipart form used by
the BlackVue Viewer, captured once from the desktop app on the same network.

## 11. Documentation (same commits as the behaviour)

- A new user guide page, `docs/guide/camera-settings.md`, added to `mkdocs.yml`
  nav: what each tab does, the format warning, recovery with the phone app, and
  the auth-mode note.
- `docs/guide/configuration.md`: the `camera` section knobs (phase C).
- `docs/api.md`: the `/api/camera/*` endpoints.
- `docs/guide/troubleshooting.md`: the camera didn't come back after a Wi-Fi
  change; the camera rejected a value.
- `docs/reference/blackvue-camera-config.md`: verified key encodings (phase B).
- CHANGELOG under `## Unreleased`, and CLAUDE.md (modules, endpoints, the
  format guard).

## 12. Out of scope

- Firmware upload (shares `upload.cgi`; never exposed).
- "FW Language" (an app tab that is not in `config.ini`).
- Models other than the DR900X Plus beyond the generic "Other" fallback.
- Scheduling settings changes.

## 13. Open points for the spec review

1. **Password encryption** (section 3): adding `cryptography` as a dependency
   and embedding the published BlackVue key, so reveal and write work.
2. **Value codes** still marked "Kumar" in Appendix A (`AutoParking`, sensitivity
   and volume ranges, alert units, half-hour time zones) stay plain number fields
   until calibration (section 10, test 0) confirms them; that does not block
   phase A.
3. `RecordTime` is marked read-only because the manual says the segment length
   is fixed at 1 minute. Change it if you want it editable anyway.
4. `ImageSetting` is format-guarded until calibration shows what it does.
5. Phase A ships the read-only panes on their own as 3.2.0. The alternative is
   holding everything for one release after phase C.

---

## Appendix A -- Key catalogue (DR900X Plus, fw 1.015)

Labels come from the manual. Value codes marked **DR900S** come from the
`DavidMetcalfe/BlackVue-DR900S-config` table (BlackVue support plus
experimentation); the X Plus is assumed to match. **Kumar** marks a code to be
confirmed from the phone app before the dropdown is offered; until then the key
is a number field. Observed values are from Kumar's camera on 2026-10-04 (non-secret
keys only).

### Basic (`[Tab1]`, 19 keys)

| Key | Label | Widget | Flags | Note |
| --- | --- | --- | --- | --- |
| `TimeSet` | Time source | select | format | DR900S: 0 = sync with GPS, 1 = manual |
| `SetTime` | Manual time | time | format | DR900S: 24-hour `HHMM` |
| `TimeZone` | Time zone | select | format | UTC offset as signed `HHMM`: `1000` = UTC+10 (confirmed by Kumar), `-1100` = UTC-11. Half-hour zones: Kumar |
| `Daylight` | Daylight saving time | toggle | format | |
| `GpsSync` | Sync time with GPS | toggle | format | |
| `ImageSetting` | Image setting | select | format | 0-indexed enum in manual order, 0 = highest (Kumar); labels confirmed by calibration |
| `VideoQuality` | Image quality | select | format | 0 = Highest (Extreme), 1 = Highest, 2 = High, 3 = Normal (manual order, 0-indexed, per Kumar); confirmed by calibration |
| `NormalRecord` | Normal recording | toggle | | |
| `AutoParking` | Parking mode | select | | Not in DR900S table: Kumar |
| `RearParkingMode` | Rear camera recording in parking mode | toggle | | |
| `VoiceRecord` | Voice recording | toggle | | |
| `DateDisplay` | Date and time display | toggle | | |
| `SpeedUnit` | Speed unit | select | | DR900S: 0 = km/h, 1 = MPH, 2 = Off |
| `RecordTime` | Video segment length | -- | read-only | fixed at 1 minute |
| `LockEvent` | Lock event files | toggle | | max 50 files |
| `OverwriteLock` | Overwrite locked event files when full | toggle | | |
| `FrontRotate` | Front camera rotation | toggle | | DR900S: 0 = off, 1 = 180° |
| `RearRotate` | Rear camera orientation | select | | DR900S: 0 = default, 1 = rotate 180°, 2 = mirror |
| `UseGpsInfo` | GPS location recording | toggle | | |

### Sensitivity (`[Tab2]`, 9 keys)

| Key | Label | Widget | Note |
| --- | --- | --- | --- |
| `NORMALSENSOR1`-`3` | G-sensor, normal mode: up/down, side to side, front/back | number | observed `5`; range, and whether 0 = off: Kumar |
| `PARKINGSENSOR1`-`3` | G-sensor, parking mode (3 axes) | number | observed `7` |
| `MOTIONSENSOR` | Motion detection, parking mode | number | observed `3` |
| `FrontMotionRegion`, `RearMotionRegion` | Motion detection regions | number | `65535` = all regions; a grid editor is out of scope |

### System (`[Tab3]`, 45 keys)

| Key(s) | Label | Widget | Note |
| --- | --- | --- | --- |
| `RECLED` | Recording status LED | toggle | |
| `NORMALLED`, `PARKINGLED` | Front security LED (normal / parking) | toggle | |
| `RearLED`, `LTELED`, `WifiLED`, `BTLED` | Rear security / LTE / Wi-Fi / Bluetooth LED | toggle | |
| `PSENSOR` | Proximity sensor | select | DR900S: 0 = voice recording on/off, 1 = manual recording, 2 = off |
| `STARTVOICE` ... `PARKINGEVENTVOICE` (12) | Voice guidance items | toggle | one toggle per announcement |
| `VOLUME` | Volume | number | observed `5`; range: Kumar |
| `ScheduledReboot`, `ScheduledRebootTime` | Scheduled reboot, time | toggle, time | observed hour `3` (03:00) |
| `EventSpeedUnit` | Speed alert unit | select | Kumar (DR900S used 0 = km/h, 1 = MPH, 2 = off for its `SpeedAlert`) |
| `AlertLimit` | Speed alert | number | up to 300 km/h / 200 MPH |
| `Battery` | Battery protection | toggle | hardwired only |
| `LowvoltageTime` | Low-voltage cut-off timer | number | 0 = off, else hours until power-off: up to 12 on 12 V, 48 on 24 V (Kumar) |
| `LowvoltageVolt`, `LowvoltageVoltHeavy` | Low-voltage cut-off voltage (12 V / 24 V) | number | `1190` = 11.9 V, `2320` = 23.2 V (scale 0.01) |
| `userString` | User text overlay | text | up to 20 characters |
| `AccelLimit`, `HarshLimit`, `SharpLimit` | Acceleration / harsh braking / sharp turn alert thresholds | number | units and range: Kumar |
| `BTPair` | Bluetooth pairing | toggle | |
| `Dsm*` (11) | Driver monitoring options | number | present in the file but no DMS hardware on this unit (Kumar): shown under a collapsed "Other (not fitted)" group, still settable |

### Wi-Fi (`[Wifi]`, 4 keys)

| Key | Label | Widget | Flags |
| --- | --- | --- | --- |
| `ap_ssid` | Camera hotspot name | text | |
| `ap_pw` | Camera hotspot password | password | secret, 8-63 |
| `WiFiBand` | Wi-Fi band | select | DR900S: 0 = 5 GHz, 1 = 2.4 GHz |
| `WifiSleepMode` | Wi-Fi auto turn off | toggle | |

### Cloud (`[Cloud]`, 8 keys)

| Key | Label | Widget | Flags |
| --- | --- | --- | --- |
| `CloudService` | Cloud service | toggle | |
| `sta_ssid`, `sta2_ssid`, `sta3_ssid` | Home network 1 / 2 / 3 name | text | tried in order 1, 2, 3 |
| `sta_pw`, `sta2_pw`, `sta3_pw` | Home network 1 / 2 / 3 password | password | secret; may be empty |
| `CloudSettingVersion` | Cloud settings version | -- | read-only |
