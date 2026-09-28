/* app.js: wires the CSRF token into every htmx request via the X-CSRFToken header */

document.body.addEventListener("htmx:configRequest", (event) => {
  const tokenEl = document.querySelector('meta[name="csrf-token"]');
  if (tokenEl) {
    event.detail.headers["X-CSRFToken"] = tokenEl.content;
  }
});
