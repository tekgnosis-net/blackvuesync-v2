# Camera config fixtures

`dr900x-plus-config.ini` reproduces the `Config/config.ini` of a BlackVue
DR900X Plus (firmware 1.015, config version 1.071): the same sections, key order
and non-secret settings as a real camera, read on 2026-10-05.

`dr900x-plus-version.bin` is the same camera's `Config/version.bin` (no personal
data in it).

Everything personal is invented:

| Key | Fixture value |
| --- | --- |
| `ap_ssid` | `Blackvue900XPlus-000000` |
| `sta_ssid`, `sta2_ssid`, `sta3_ssid` | `DemoHome`, `DemoGarage`, `DemoPhone` |
| `userString` | `DEMO01` |
| `CloudSettingVersion` | `2026-01-01 00:00:00` |

The `*_pw` values are invented passwords encrypted the way the camera stores
them (AES-128-CBC with BlackVue's fixed key and IV, zero-padded to 32 bytes,
uppercase hex), so decryption can be tested against known plaintexts:

| Key | Plaintext |
| --- | --- |
| `ap_pw` | `demo-cam` |
| `sta_pw` | `DemoHome-123` |
| `sta2_pw` | `DemoGarage-2` |
| `sta3_pw` | `DemoPhone-33` |

Never replace this file with a real camera's `config.ini`: the encryption key is
public, so real passwords committed here would be readable by anyone.
