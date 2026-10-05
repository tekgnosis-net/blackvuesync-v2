"""tests for the /hx/camera/panes fragment and the settings page nav."""

# ruff: noqa: ARG001, F811

from __future__ import annotations

from typing import Any

from test_routes_api_camera import CONFIG, FakeCamera, camera, client  # noqa: F401

MASK = "•" * 8


def test_panes_render_every_tab_read_only(client: Any, camera: FakeCamera) -> None:
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    for name in ("basic", "sensitivity", "system", "wifi", "cloud"):
        assert f'data-pane="camera-{name}"' in html
    assert "DR900X Plus · fw 1.015" in html
    assert "UTC+10:00" in html and "Highest (Extreme)" in html
    assert "erases camera recordings" in html
    assert 'data-reveal="Cloud.sta_pw"' in html and MASK in html
    assert "1E1BC3E1" not in html and "DemoHome-123" not in html
    assert "Driver monitoring" in html  # collapsed group, still listed


def test_panes_show_the_offline_banner(client: Any, camera: FakeCamera) -> None:
    client.get("/hx/camera/panes")
    camera.files = {}
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "Camera offline: showing settings read at" in html


def test_panes_before_the_first_read(client: Any, camera: FakeCamera) -> None:
    camera.files = {}
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "hasn't been reached yet" in html


def test_panes_list_changes_on_the_camera(client: Any, camera: FakeCamera) -> None:
    client.get("/hx/camera/panes")
    camera.files["/Config/config.ini"] = CONFIG.replace(b"VOLUME=5", b"VOLUME=4")
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "changed on the camera" in html and "Tab3.VOLUME" in html


def test_non_utf8_ssid_renders(client: Any, camera: FakeCamera) -> None:
    """review focus 1: a latin-1 byte must not break the fragment."""
    camera.files["/Config/config.ini"] = CONFIG.replace(b"DemoHome", b"Caf\xe9")
    resp = client.get("/hx/camera/panes")
    assert resp.status_code == 200
    assert "Caf�" in resp.get_data(as_text=True)


def test_dashboard_card_links_to_the_camera_settings(
    client: Any, camera: FakeCamera, monkeypatch: Any
) -> None:
    import urllib.request

    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("car away")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    html = client.get("/hx/dashcam-info-card").get_data(as_text=True)
    assert 'href="/settings#camera-basic"' in html


def test_settings_page_has_the_camera_nav(client: Any, camera: FakeCamera) -> None:
    html = client.get("/settings").get_data(as_text=True)
    assert 'data-section-nav="camera-basic"' in html
    assert 'id="camera-panes"' in html and 'hx-get="/hx/camera/panes"' in html
    assert "Camera access" in html  # the app-settings section from task 5


def test_change_in_non_utf8_section_renders(client: Any, camera: FakeCamera) -> None:
    client.get("/hx/camera/panes")
    camera.files["/Config/config.ini"] = CONFIG + b"\n[Caf\xe9]\nMode=1\n"
    resp = client.get("/hx/camera/panes")
    assert resp.status_code == 200
    assert "�" in resp.get_data(as_text=True)
