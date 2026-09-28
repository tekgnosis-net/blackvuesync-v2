// settings.js: alpine (@alpinejs/csp) component for the settings page. owns
// section navigation, typed per-section save (json with correct types), the
// tier toast, the auth-mode confirm, and the change-password dialog.

const TOAST_MS = 4000;

function csrfToken() {
  const el = document.querySelector('meta[name="csrf-token"]');
  return el ? el.content : "";
}

function redirectToLogin() {
  location.assign("/login?next=" + encodeURIComponent(location.pathname));
}

// parses a json body; null when the response is not json or is malformed.
async function readJson(resp) {
  const type = resp.headers.get("Content-Type") || "";
  if (!type.includes("application/json")) return null;
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

// true when the session expired: a 401 AUTH_REQUIRED, or a followed redirect
// to /login. other 401s (e.g. a wrong current password) are not auth failures.
function isAuthFailure(resp, data) {
  if (resp.redirected && new URL(resp.url).pathname === "/login") return true;
  return resp.status === 401 && (!data || data.code === "AUTH_REQUIRED");
}

// the json "error" field when present, else the HTTP status.
function failureText(resp, data) {
  return data?.error ? String(data.error) : "HTTP " + resp.status;
}

// builds a typed json object from a section/form's [data-field] inputs.
function collectFields(root) {
  const out = {};
  root.querySelectorAll("[data-field]").forEach((el) => {
    const name = el.dataset.field;
    const type = el.dataset.type;
    if (type === "bool") {
      out[name] = el.checked;
    } else if (type === "number") {
      out[name] = el.value === "" ? null : Number(el.value);
    } else if (type === "lines") {
      out[name] = el.value
        .split("\n")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
    } else if (type === "letters") {
      out[name] = Array.from(el.querySelectorAll("input:checked")).map(
        (c) => c.value
      );
    } else {
      out[name] = el.value;
    }
  });
  return out;
}

document.addEventListener("alpine:init", () => {
  Alpine.data("settingsPage", () => ({
    init() {
      // without js, all panes show (one scroll); js-nav switches to single-pane.
      this.$root.classList.add("js-nav");
      this.activate(this.$root.dataset.initial || "");
    },

    // toggles the .is-active pane and the .active nav item imperatively (no
    // per-element getters -- the csp build only sees bare @click="method" refs).
    activate(section) {
      this.$root.querySelectorAll("[data-pane]").forEach((p) => {
        p.classList.toggle("is-active", p.dataset.pane === section);
      });
      this.$root.querySelectorAll("[data-section-nav]").forEach((n) => {
        n.classList.toggle("active", n.dataset.sectionNav === section);
      });
    },

    select(ev) {
      this.activate(ev.currentTarget.dataset.sectionNav);
    },

    showToast(section, text) {
      const el = this.$root.querySelector(`[data-toast="${section}"]`);
      if (!el) return;
      el.textContent = text;
      el.classList.add("ok");
      el.hidden = false;
      setTimeout(() => {
        el.hidden = true;
      }, TOAST_MS);
    },

    setErrors(key, messages) {
      const el = this.$root.querySelector(`[data-errors="${key}"]`);
      if (!el) return;
      el.textContent = messages.join("; ");
      el.hidden = messages.length === 0;
    },

    async save(ev) {
      const section = ev.currentTarget.dataset.save;
      const form = this.$root.querySelector(`[data-form="${section}"]`);
      const payload = collectFields(form);
      if (section === "auth" && !this.confirmModeChange(payload)) return;
      const resp = await this.send(`/api/settings/${section}`, payload, "PATCH");
      if (!resp) {
        this.setErrors(section, ["save failed (network error); please retry"]);
        return;
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
      } else if (resp.status === 200 && data) {
        this.setErrors(section, []);
        this.showToast(section, this.tierMessage(data.tier));
      } else if (resp.status === 422 && data) {
        const messages = (data.details?.field_errors || []).map((e) => e.message);
        this.setErrors(section, messages.length ? messages : [failureText(resp, data)]);
      } else {
        this.setErrors(section, [
          "save failed (" + failureText(resp, data) + "); please retry",
        ]);
      }
    },

    tierMessage(tier) {
      if (tier === "immediate") return "Saved.";
      if (tier === "next_tick") return "Saved -- applies at the next sync.";
      return "Saved -- restart the container to take effect.";
    },

    confirmModeChange(payload) {
      // only guard when the auth mode actually differs from the rendered value.
      const select = this.$root.querySelector(
        '[data-form="auth"] [data-field="mode"]'
      );
      const original = select ? select.dataset.original : null;
      if (original !== null && payload.mode !== original) {
        return globalThis.confirm(
          "Changing the auth mode can affect your own access. Continue?"
        );
      }
      return true;
    },

    openPasswordDialog() {
      this.$refs.pwDialog.showModal();
    },

    closePasswordDialog() {
      this.setErrors("password", []);
      this.$refs.pwDialog.close();
    },

    async submitPassword() {
      const form = this.$root.querySelector('[data-form="password"]');
      const f = collectFields(form);
      if (f.new_password !== f.confirm_password) {
        this.setErrors("password", [
          "new password and confirmation do not match",
        ]);
        return;
      }
      const resp = await this.send(
        "/api/auth/password",
        { current_password: f.current_password, new_password: f.new_password },
        "POST"
      );
      if (!resp) {
        this.setErrors("password", ["could not change password (network error)"]);
        return;
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
      } else if (resp.status === 200) {
        this.setErrors("password", []);
        this.$refs.pwDialog.close();
      } else if (resp.status === 422 && data) {
        const messages = (data.details?.field_errors || []).map((e) => e.message);
        this.setErrors("password", messages.length ? messages : [failureText(resp, data)]);
      } else if (resp.status === 401) {
        this.setErrors("password", ["current password is incorrect"]);
      } else {
        this.setErrors("password", [
          "could not change password (" + failureText(resp, data) + ")",
        ]);
      }
    },

    async rotateSessions() {
      if (
        !globalThis.confirm(
          "Sign out all sessions? Everyone, including you, will need to log in again."
        )
      )
        return;
      const resp = await this.send("/api/auth/sessions", null, "DELETE");
      if (!resp) {
        this.setErrors("auth", ["could not rotate sessions (network error)"]);
        return;
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
      } else if (resp.ok) {
        // rotation also ends this session, so the next page needs a login.
        redirectToLogin();
      } else {
        this.setErrors("auth", [
          "could not rotate sessions (" + failureText(resp, data) + ")",
        ]);
      }
    },

    async send(path, payload, method) {
      try {
        return await fetch(path, {
          method: method,
          headers: {
            "X-CSRFToken": csrfToken(),
            "Content-Type": "application/json",
          },
          body: payload === null ? undefined : JSON.stringify(payload),
        });
      } catch {
        // network error; caller surfaces a retry message
        return null;
      }
    },
  }));
});
