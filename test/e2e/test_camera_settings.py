"""browser tests for the read-only camera settings panes."""

from __future__ import annotations

from pathlib import Path
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
def test_hiding_a_revealed_password_empties_an_unedited_field(
    live_server: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "cloud")
    field = page.locator('[data-camera-key="Cloud.sta_pw"]')
    button = page.locator('[data-reveal="Cloud.sta_pw"]')
    button.click()
    expect(field).to_have_value("DemoHome-123")
    button.click()
    expect(field).to_have_value("")
    expect(page.locator("#camera-apply-bar")).to_be_hidden()
    # an edited value survives hiding
    button.click()
    field.fill("Another-pass-1")
    field.dispatch_event("change")
    button.click()
    expect(field).to_have_value("Another-pass-1")
    expect(page.locator("#camera-apply-bar [data-camera-count]")).to_have_text(
        "1 change"
    )


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


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/camera/dr900x-plus-config.ini"


def _stage(page: Page, tab: str, key: str, value: str) -> None:
    page.locator(f'[data-section-nav="camera-{tab}"]').click()
    control = page.locator(f'[data-pane="camera-{tab}"] [data-camera-key="{key}"]')
    control.fill(value)
    control.dispatch_event("change")


def _stage_timezone(page: Page) -> None:
    """stages an offset that differs from the fixture's 1000 and proves it took."""
    _stage(page, "basic", "Tab1.TimeZone", "0930")
    control = page.locator('[data-camera-key="Tab1.TimeZone"]')
    expect(control).to_have_value("0930")
    expect(page.locator("#camera-apply-bar [data-camera-count]")).to_have_text(
        "1 change"
    )


def _apply_one_change(page: Page, base: str) -> None:
    _open_camera_tab(page, base, "system")
    _stage(page, "system", "Tab3.VOLUME", "4")
    page.get_by_role("button", name="Review & apply").click()
    dialog = page.locator("#camera-review")
    dialog.locator("[data-review-apply]").click()
    expect(dialog.locator("[data-review-result]")).to_contain_text("Applied")
    dialog.locator("[data-review-cancel]").click()


def test_stage_review_apply(live_server: Any, fake_camera: Any, page: Page) -> None:
    _open_camera_tab(page, live_server.url, "system")
    _stage(page, "system", "Tab3.VOLUME", "4")
    _stage(page, "cloud", "Cloud.sta2_ssid", "Shed")
    bar = page.locator("#camera-apply-bar")
    expect(bar.locator("[data-camera-count]")).to_have_text("2 changes")
    bar.get_by_role("button", name="Review & apply").click()
    dialog = page.locator("#camera-review")
    expect(dialog.locator("[data-review-rows] tr")).to_have_count(2)
    dialog.locator("[data-review-apply]").click()
    expect(dialog.locator("[data-review-result]")).to_contain_text(
        "Applied and verified"
    )
    dialog.locator("[data-review-cancel]").click()
    # a reload proves the server-rendered originals changed, not just the inputs
    page.reload()
    expect(page.locator("#camera-panes .camera-field").first).to_be_attached()
    page.locator('[data-section-nav="camera-system"]').click()
    volume = page.locator('[data-pane="camera-system"] [data-camera-key="Tab3.VOLUME"]')
    expect(volume).to_have_attribute("data-original", "4")
    expect(volume).to_have_value("4")
    page.locator('[data-section-nav="camera-cloud"]').click()
    ssid = page.locator(
        '[data-pane="camera-cloud"] [data-camera-key="Cloud.sta2_ssid"]'
    )
    expect(ssid).to_have_attribute("data-original", "Shed")
    assert b"VOLUME=4" in fake_camera.config
    assert b"sta2_ssid=Shed" in fake_camera.config


def test_format_key_warns_about_pending_recordings(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    fake_camera.vod = ["20261027_210000_NF.mp4"]
    before = fake_camera.config
    _open_camera_tab(page, live_server.url, "basic")
    _stage_timezone(page)
    page.get_by_role("button", name="Review & apply").click()
    dialog = page.locator("#camera-review")
    expect(dialog.locator("[data-review-pending]")).to_contain_text("1 recording")
    apply = dialog.locator("[data-review-apply]")
    expect(apply).to_be_disabled()
    dialog.locator("[data-confirm-format]").check()
    expect(apply).to_be_disabled()
    dialog.locator("[data-accept-loss]").check()
    expect(apply).to_be_enabled()
    dialog.locator("[data-review-cancel]").click()
    assert fake_camera.config == before


def test_format_key_needs_the_checkbox_when_nothing_is_pending(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    fake_camera.vod = []
    _open_camera_tab(page, live_server.url, "basic")
    _stage_timezone(page)
    page.get_by_role("button", name="Review & apply").click()
    dialog = page.locator("#camera-review")
    expect(dialog.locator("[data-review-loss]")).to_be_hidden()
    apply = dialog.locator("[data-review-apply]")
    expect(apply).to_be_disabled()
    dialog.locator("[data-confirm-format]").check()
    expect(apply).to_be_enabled()


@pytest.mark.usefixtures("fake_camera")
def test_discard_resets_fields_and_hides_the_bar(live_server: Any, page: Page) -> None:
    _open_camera_tab(page, live_server.url, "system")
    _stage(page, "system", "Tab3.VOLUME", "4")
    bar = page.locator("#camera-apply-bar")
    expect(bar).to_be_visible()
    bar.get_by_role("button", name="Discard").click()
    expect(bar).to_be_hidden()
    expect(page.locator('[data-camera-key="Tab3.VOLUME"]')).to_have_value("5")


def test_backups_pane_lists_and_restores(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _apply_one_change(page, live_server.url)
    assert b"VOLUME=4" in fake_camera.config
    page.locator('[data-section-nav="camera-backups"]').click()
    pane = page.locator('[data-pane="camera-backups"]')
    rows = pane.locator(".camera-backup")
    expect(rows).to_have_count(1)
    rows.first.locator("button[data-restore]").click()
    dialog = page.locator("#camera-review")
    expect(dialog.locator("[data-review-title]")).to_contain_text(".ini")
    expect(dialog).to_have_attribute("data-state", "ready")
    # the table lists the key the backup would change back
    expect(dialog.locator("[data-review-rows] tr")).to_have_count(1)
    expect(dialog.locator("[data-review-rows] tr").first).to_contain_text("Volume")
    expect(dialog.locator("[data-review-rows] tr").first).to_contain_text("4")
    dialog.locator("[data-review-apply]").click()
    expect(dialog).to_have_attribute("data-state", "done")
    expect(dialog.locator("[data-review-result]")).to_contain_text("Applied")
    assert fake_camera.config == FIXTURE.read_bytes()


@pytest.mark.usefixtures("fake_camera")
def test_restore_with_nothing_to_change_says_so(live_server: Any, page: Page) -> None:
    _apply_one_change(page, live_server.url)
    page.locator('[data-section-nav="camera-backups"]').click()
    page.locator(".camera-backup button[data-restore]").first.click()
    dialog = page.locator("#camera-review")
    dialog.locator("[data-review-apply]").click()
    expect(dialog).to_have_attribute("data-state", "done")
    dialog.locator("[data-review-cancel]").click()
    # the camera now equals the original backup, so restoring it changes nothing
    page.locator(".camera-backup button[data-restore]").last.click()
    expect(dialog.locator("[data-review-result]")).to_contain_text("Nothing to restore")
    expect(dialog).to_have_attribute("data-state", "done")
    expect(dialog.locator("[data-review-apply]")).to_be_hidden()


def test_a_dropped_upload_that_did_not_apply_says_so(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    fake_camera.drop_uploads = True
    _open_camera_tab(page, live_server.url, "system")
    _stage(page, "system", "Tab3.VOLUME", "4")
    page.get_by_role("button", name="Review & apply").click()
    dialog = page.locator("#camera-review")
    dialog.locator("[data-review-apply]").click()
    result = dialog.locator("[data-review-result]")
    expect(result).to_contain_text("did not take effect", timeout=30000)
    expect(result).not_to_contain_text("Applied")
    assert b"VOLUME=4" not in fake_camera.config
