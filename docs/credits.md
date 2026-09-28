# Credits

## The original BlackVue Sync

BlackVue Sync v2 exists because of **[Alessandro Colomba](https://github.com/acolomba)**,
who created [BlackVue Sync](https://github.com/acolomba/blackvuesync) in October
2018 and has maintained it since. Thank you, Alessandro.

The part of v2 that talks to the dashcam is his design and, largely, his code:

- parsing BlackVue recording file names (types, camera directions, upload flags),
- the download loop, retries after failures, and the destination lock,
- retention, date grouping and the disk-usage safety stop,
- cron mode, the command line, and the Docker packaging it grew from.

Upstream contributors whose work is part of this code base:

| Contributor | Contribution |
| --- | --- |
| [Karl Q.](https://github.com/kquinsland01) | Structured JSON logs and Prometheus metrics export ([#73](https://github.com/acolomba/blackvuesync/pull/73), [#74](https://github.com/acolomba/blackvuesync/pull/74)) |
| [Chris Carini](https://github.com/ChrisCarini) | Handling of `socket.timeout` while listing recordings ([#75](https://github.com/acolomba/blackvuesync/pull/75)) |
| [komputerking](https://github.com/komputerking) | Interior camera support for the DR750X-3CH ([#7](https://github.com/acolomba/blackvuesync/pull/7)) |
| [Sathia](https://github.com/sathia-musso) | The original recording filter option ([#6](https://github.com/acolomba/blackvuesync/pull/6)) |
| [grysage/blackvuesync](https://github.com/grysage/blackvuesync) | The idea for the Optional (`O`) camera direction |

## How v2 relates to the original

v2 started in May 2026 as a fork of the upstream project (upstream commit
`7871a77`) and became a separate project with release 3.0.0. The full git
history, including every upstream commit, is kept in this repository.

v2 adds a long-running web service around the sync engine: scheduling,
authentication, a live dashboard, a settings editor, a log viewer, statistics
and a recording viewer. See [Features](features.md) and the
[release notes](release-notes.md).

The two projects do not share a package name, a command or an image, so both
can be installed on the same machine:

| | Upstream | v2 |
| --- | --- | --- |
| Repository | [acolomba/blackvuesync](https://github.com/acolomba/blackvuesync) | [tekgnosis-net/blackvuesync-v2](https://github.com/tekgnosis-net/blackvuesync-v2) |
| Command | `blackvuesync` | `blackvuesync-v2` |
| Python package | `blackvuesync` (PyPI) | `blackvuesync-v2` |
| Docker image | upstream's image | `ghcr.io/tekgnosis-net/blackvuesync-v2` |

They deliberately share the destination lock file (`.blackvuesync.lock`), so
an upstream cron job and v2 never download into the same directory at the
same time.

The development history of the fork phase (pull requests #1 to #22) stays
readable in the archived repository
[tekgnosis-net/blackvuesync](https://github.com/tekgnosis-net/blackvuesync).

## Third-party software

The web interface bundles [Alpine.js](https://alpinejs.dev/),
[htmx](https://htmx.org/), [Chart.js](https://www.chartjs.org/) and
[Leaflet](https://leafletjs.com/) (all MIT or BSD licensed; see
`blackvuesync_v2/server/static/js/VENDORED.md`). Map tiles and the demo route
in the screenshots are © [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors.

## License

MIT, like the original. The original copyright notice is kept unchanged:

```text
Copyright 2018-2026 Alessandro Colomba
v2 additions copyright 2026 tekgnosis-net, under the same MIT license.
```

See [`COPYING`](https://github.com/tekgnosis-net/blackvuesync-v2/blob/main/COPYING).
