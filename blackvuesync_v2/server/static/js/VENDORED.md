# Vendored JavaScript libraries

| File | Library | Version | Source URL |
| --- | --- | --- | --- |
| `htmx.min.js` | HTMX | 2.0.6 | <https://unpkg.com/htmx.org@2.0.6/dist/htmx.min.js> |
| `alpine.min.js` | Alpine.js CSP build | 3.14.9 | <https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.14.9/dist/cdn.min.js> |
| `leaflet.js` | Leaflet | 1.9.4 | <https://unpkg.com/leaflet@1.9.4/dist/leaflet.js> |
| `chart.umd.min.js` | Chart.js | 4.4.6 | <https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js> |

These files are checked in to avoid a CDN dependency at runtime.
To update, download the new version and replace the file; update the
version number in this table and in `pyproject.toml`'s comment (if any).
