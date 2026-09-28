"""playwright smoke for the /stats page."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e


def _login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', "pw-1234-test")
    page.click('button[type="submit"]')


def test_stats_page_loads_switches_range_no_js_errors(
    live_server: Any, page: Page
) -> None:  # type: ignore[no-untyped-def]
    base = live_server.url
    _login(page, base)

    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    # the initial page load triggers the 7d series fetch; wait for it so the
    # first chart render has run before interacting.
    with page.expect_response(lambda r: "/api/stats/series" in r.url):
        page.goto(f"{base}/stats")

    expect(page.locator(".stats-page")).to_be_visible()
    expect(page.locator('.stats-range-btn[data-range="30d"]')).to_be_visible()
    expect(page.locator('[data-chart="disk"]')).to_be_visible()

    # clicking 30d triggers a new series fetch; wait for that specific response.
    with page.expect_response(lambda r: "range=30d" in r.url):
        page.click('.stats-range-btn[data-range="30d"]')

    expect(page.locator('.stats-range-btn.active[data-range="30d"]')).to_be_visible()

    # let the synchronous renderCharts() that runs after the response settle,
    # then assert no uncaught js errors fired at any point.
    page.wait_for_load_state("networkidle")
    assert errors == [], f"uncaught page errors: {errors}"


def _series(summary: dict[str, Any], points: list[dict[str, Any]]) -> str:
    import json

    return json.dumps(
        {
            "range": "7d",
            "summary": summary,
            "series": {"points": points},
            "forecast": {"projected": [], "limits": {}},
        }
    )


def _point(ts: int, offline: bool, failures: dict[str, int]) -> dict[str, Any]:
    return {
        "ts": ts, "bytes": 0, "files": 0, "duration": 3.0, "disk": 0.5,
        "success": 0 if failures else 1, "failures": failures, "dry_run": 0,
        "offline": offline,
    }  # fmt: skip


def test_offline_tile_and_offline_series(live_server: Any, page: Page) -> None:
    body = _series(
        {
            "runs": 3,
            "offline": 2,
            "reachable_runs": 1,
            "bytes": 0,
            "avg_duration_seconds": 42.0,
            "success_rate": 1.0,
        },  # fmt: skip
        [
            _point(1_700_000_000, False, {}),
            _point(1_700_000_900, True, {"network": 1, "http": 0}),
            _point(1_700_001_800, True, {"timeout": 1}),
        ],
    )
    page.route(
        "**/api/stats/series*",
        lambda r: r.fulfill(status=200, content_type="application/json", body=body),
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/stats")
    expect(page.locator("[data-summary-offline]")).to_have_text("2")
    expect(page.locator("[data-summary-success]")).to_have_text("100.0%")
    expect(page.locator("[data-summary-runs]")).to_have_text("3")
    datasets = page.evaluate(
        "() => Chart.getChart(document.querySelector('[data-chart=\"failures\"]'))"
        ".data.datasets.map(d => [d.label, d.data])"
    )
    by_label = dict(datasets)
    assert by_label["dashcam offline"] == [0, 1, 1]
    assert by_label["network"] == [0, 0, 0]  # offline runs are not failures
    assert by_label["timeout"] == [0, 0, 0]


def test_success_rate_shows_dash_when_nothing_reached_the_dashcam(
    live_server: Any, page: Page
) -> None:
    body = _series(
        {
            "runs": 2,
            "offline": 2,
            "reachable_runs": 0,
            "bytes": 0,
            "avg_duration_seconds": 0.0,
            "success_rate": None,
        },  # fmt: skip
        [_point(1_700_000_000, True, {"network": 1})],
    )
    page.route(
        "**/api/stats/series*",
        lambda r: r.fulfill(status=200, content_type="application/json", body=body),
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/stats")
    expect(page.locator("[data-summary-offline]")).to_have_text("2")
    expect(page.locator("[data-summary-success]")).to_have_text("--")


def test_every_failure_reason_has_its_own_colour(live_server: Any, page: Page) -> None:
    """an explicitly coloured series disables chart.js's automatic palette for
    the whole chart, so each reason must carry its own colour."""
    body = _series(
        {
            "runs": 2,
            "offline": 1,
            "reachable_runs": 1,
            "bytes": 0,
            "avg_duration_seconds": 1.0,
            "success_rate": 0.0,
        },  # fmt: skip
        [
            _point(
                1_700_000_000,
                False,
                {"http": 1, "network": 1, "timeout": 1, "disk": 1, "unknown": 1},
            ),
            _point(1_700_000_900, True, {"network": 1}),
        ],  # fmt: skip
    )
    page.route(
        "**/api/stats/series*",
        lambda r: r.fulfill(status=200, content_type="application/json", body=body),
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/stats")
    expect(page.locator("[data-summary-offline]")).to_have_text("1")
    # the colours chart.js resolved for the drawn bars, not just the config
    colours = dict(
        page.evaluate(
            "() => { const c = Chart.getChart(document.querySelector("
            "'[data-chart=\"failures\"]'));"
            " return c.data.datasets.map((d, i) =>"
            " [d.label, c.getDatasetMeta(i).data[0].options.backgroundColor]); }"
        )
    )
    reasons = [colours[r] for r in ("http", "network", "timeout", "disk", "unknown")]
    assert len(set(reasons)) == 5, colours
    assert "rgba(0,0,0,0.1)" not in reasons  # chart.js's uncoloured default
    assert colours["dashcam offline"] not in reasons
