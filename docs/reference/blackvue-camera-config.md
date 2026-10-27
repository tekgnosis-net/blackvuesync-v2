# Camera configuration reference

Value codes for `config.ini` keys on the BlackVue DR900X Plus, and the
endpoints the camera exposes for reading and changing settings.

Source: DR900X Plus, firmware 1.015, calibrated 2026-10-27 by changing one
setting at a time in the BlackVue app and diffing `config.ini`.

## Calibrated value codes

| Key | Codes |
| --- | --- |
| `Tab1.AutoParking` | 1 = Motion & Impact detection, 2 = Time lapse |
| `Tab1.SpeedUnit` | 0 = km/h, 1 = MPH (2 = Off, DR900S table) |
| `Tab2.NORMALSENSOR1-3`, `Tab2.PARKINGSENSOR1-3` | 0-10 |
| `Tab2.MOTIONSENSOR` | 0-10 |
| `Tab3.VOLUME` | 0-10 |
| `Tab3.AlertLimit` | number in the display unit |
| `Tab3.PSENSOR` | 0 = Off, 1 = Manual recording, 2 = Voice recording (differs from the DR900S table) |
| `Tab3.LowvoltageVolt`, `Tab3.LowvoltageVoltHeavy` | volts x 100 (1180 = 11.8 V) |
| `Wifi.WiFiBand` | 0 = 5 GHz, 1 = 2.4 GHz |

`Tab1.TimeZone` is a signed HHMM offset from UTC (the fixture holds `1000`). The
firmware's own list was never calibrated, so the settings page does not
enumerate one: it validates the text instead (-1200 to +1400, minutes 00, 30
or 45). `Tab1.SetTime` is a 24-hour `HHMM` time.

## Endpoints

- `GET /Config/config.ini` and `GET /Config/version.bin` need no
  authentication.
- `GET /upload.cgi` returns an HTML form. It shows that settings are changed
  by `POST /upload.cgi` with `multipart/form-data`, a `FileType` field
  (values `config`, `version`, `fwupgrade`) and a file field `FileData`.
- The BlackVue app sends only `FileType=config` with a file named
  `config.ini`. The same endpoint accepts firmware, which the app never sends.

## Port 9771

The camera accepts TCP connections on port 9771. This is the FOTA service
described in the public 2023 BlackVue CVE research
(<https://github.com/eyJhb/blackvue-cve-2023>): ping, restart and firmware
upload without authentication. It is documented there for the DR750 LTE and
was observed open on this DR900X Plus. BlackVue Sync v2 does not use it. A
firewall rule that limits camera access to the sync host is recommended.
