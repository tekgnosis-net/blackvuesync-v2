"""text contrast audit: every page, light and dark, meets wcag aa (4.5:1).

colours are computed the way they render: translucent text is blended over
its background, and translucent backgrounds (tinted badges) are layered over
the opaque surface beneath them. a gallery of state-dependent styles (badges,
tiers, alerts, active buttons) is injected so styles that only appear in
certain states are audited too. leaflet's own map chrome is excluded.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Page  # noqa: E402

pytestmark = pytest.mark.e2e

MIN_RATIO = 4.5
PAGES = ("/", "/viewer", "/stats", "/logs", "/settings")

# state-dependent styles that the default page state does not show
GALLERY = """() => {
  const host = document.createElement('div');
  host.className = 'card contrast-gallery';
  host.innerHTML = [
    '<span class="badge badge-complete">ok</span>',
    '<span class="badge badge-failed">failed</span>',
    '<span class="badge badge-running">running</span>',
    '<span class="badge badge-offline">dashcam not reachable</span>',
    '<span class="settings-tier settings-tier-immediate">immediate</span>',
    '<span class="settings-tier settings-tier-next_tick">next_tick</span>',
    '<span class="settings-tier settings-tier-restart">restart</span>',
    '<div class="alert alert-info">info</div>',
    '<div class="alert alert-warning">warning</div>',
    '<div class="alert alert-error">error</div>',
    '<div class="settings-errors">error</div>',
    '<div class="settings-toast ok">saved</div>',
    '<button class="button button-primary">Primary</button>',
    '<button class="button button-secondary">Secondary</button>',
  ].join('');
  // viewer.css is only loaded on the viewer; elsewhere these would be unstyled
  if (document.getElementById('viewer-app')) {
    host.innerHTML += '<button class="viewer-btn">Swap</button>'
      + '<button class="viewer-rec active"><span>08:05</span></button>';
  }
  document.querySelector('main, .content, body').prepend(host);
}"""

_AUDIT_JS = """() => {
  // color-mix() computes to color(srgb r g b / a) with 0-1 channels
  const parse = (c) => { const m = c.match(/[\\d.]+/g) || ['0', '0', '0', '0'];
    const k = c.startsWith('color(') ? 255 : 1;
    return { r: m[0] * k, g: m[1] * k, b: m[2] * k, a: m.length > 3 ? +m[3] : 1 }; };
  const over = (top, base) => ({ r: top.r * top.a + base.r * (1 - top.a),
    g: top.g * top.a + base.g * (1 - top.a), b: top.b * top.a + base.b * (1 - top.a), a: 1 });
  const lum = (c) => { const f = (v) => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const background = (el) => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c.a > 0) { layers.push(c); if (c.a >= 1) break; }
    }
    let base = parse(getComputedStyle(document.documentElement).backgroundColor);
    if (base.a < 1) base = parse(getComputedStyle(document.body).backgroundColor);
    if (base.a < 1) base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i -= 1) base = over(layers[i], base);
    return base;
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('.leaflet-control-container, .leaflet-pane')) continue;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const ownText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!ownText && !['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) continue;
    if (el.tagName === 'INPUT' && ['range', 'checkbox', 'radio', 'hidden'].includes(el.type)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    const bg = background(el);
    const fg = over(parse(cs.color), bg);
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    if (ratio < __MIN_RATIO__) {
      const cls = typeof el.className === 'string' && el.className ? '.' + el.className.split(' ')[0] : '';
      out.push(el.tagName.toLowerCase() + cls + ' "' + (el.innerText || el.value || '').trim().slice(0, 20)
        + '" ' + ratio.toFixed(2) + ':1');
    }
  }
  return out;
}"""
AUDIT = _AUDIT_JS.replace("__MIN_RATIO__", str(MIN_RATIO))


def _login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', "pw-1234-test")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/")


def _audit_page(page: Page) -> list[str]:
    page.evaluate(GALLERY)
    findings: list[str] = page.evaluate(AUDIT)
    sections = page.locator("[data-section-nav]")
    for i in range(sections.count()):  # settings sections are shown one at a time
        sections.nth(i).click()
        findings += page.evaluate(AUDIT)
    return sorted(set(findings))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_all_text_meets_wcag_aa(
    live_server: Any, browser: Browser, scheme: str
) -> None:
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)
    page = context.new_page()
    _login(page, live_server.url)
    failures: dict[str, list[str]] = {}
    for path in PAGES:
        page.goto(live_server.url + path)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(500)
        if findings := _audit_page(page):
            failures[path] = findings
    context.close()
    assert not failures, f"text below {MIN_RATIO}:1 in {scheme} mode: {failures}"
