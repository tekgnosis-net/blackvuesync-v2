"""browser tests for the read-only camera settings panes."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e

MASK = "•" * 8


def _open_camera_tab(page: Page, base: str, tab: str) -> None:
    page.goto(f"{base}/login")
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', "pw-1234-test")
    page.click('button[type="submit"]')
    page.goto(f"{base}/settings")
    expect(page.locator("#camera-panes .camera-field").first).to_be_attached()
    page.locator(f'[data-section-nav="camera-{tab}"]').click()


@pytest.mark.usefixtures("fake_camera")
def test_reveal_shows_and_hides_one_password(live_server: Any, page: Page) -> None:
    _open_camera_tab(page, live_server.url, "cloud")
    value = page.locator('[data-secret-value="Cloud.sta_pw"]')
    button = page.locator('[data-reveal="Cloud.sta_pw"]')
    expect(value).to_have_text(MASK)
    expect(button).to_have_attribute("aria-label", "Show password")
    button.click()
    expect(value).to_have_text("DemoHome-123")
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-secret-value="Cloud.sta2_pw"]')).to_have_text(MASK)
    button.click()
    expect(value).to_have_text(MASK)
    expect(button).to_have_attribute("aria-label", "Show password")


@pytest.mark.usefixtures("fake_camera")
def test_basic_tab_shows_labels_and_the_format_warning(
    live_server: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "basic")
    pane = page.locator('[data-pane="camera-basic"]')
    expect(pane).to_be_visible()
    expect(pane).to_contain_text("UTC+10:00")
    expect(pane).to_contain_text("Highest (Extreme)")
    expect(pane.locator(".badge-format").first).to_be_visible()


@pytest.mark.usefixtures("fake_camera")
def test_settings_hash_opens_the_camera_tab(live_server: Any, page: Page) -> None:
    _open_camera_tab(page, live_server.url, "basic")
    # a hash-only goto would be a same-document navigation, not a page load
    page.goto("about:blank")
    page.goto(f"{live_server.url}/settings#camera-wifi")
    expect(page.locator('[data-section-nav="camera-wifi"]')).to_have_class(
        "settings-nav-item active"
    )
    expect(page.locator('[data-pane="camera-wifi"]')).to_be_visible()


def test_refresh_after_the_camera_leaves_shows_the_snapshot(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "system")
    fake_camera.stop()
    page.locator('[data-pane="camera-system"] button', has_text="Refresh").click()
    pane = page.locator('[data-pane="camera-system"]')
    expect(pane).to_contain_text("Camera offline: showing settings read at")
    expect(pane).to_be_visible()  # the active tab survives the swap
