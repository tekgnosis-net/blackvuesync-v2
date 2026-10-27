// settings.js: alpine (@alpinejs/csp) component for the settings page. owns
// section navigation, typed per-section save (json with correct types), the
// tier toast, the auth-mode confirm, and the change-password dialog.

const TOAST_MS = 4000;
const MASK = "•".repeat(8);

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
    current: "",
    review: { kind: "changes", changes: {}, name: "", busy: false, checking: false, done: false },

    init() {
      // without js, all panes show (one scroll); js-nav switches to single-pane.
      this.$root.classList.add("js-nav");
      // a link such as /settings#camera-basic opens that section
      const wanted = location.hash.slice(1);
      const known =
        wanted && this.$root.querySelector(`[data-section-nav="${CSS.escape(wanted)}"]`);
      this.activate(known ? wanted : this.$root.dataset.initial || "");
      // the camera panes arrive and refresh via htmx; reveal buttons are
      // delegated, and a swap re-applies the active pane.
      this.$root.addEventListener("click", (ev) => {
        const button = ev.target.closest("[data-reveal]");
        if (button) this.toggleSecret(button);
      });
      document.body.addEventListener("htmx:afterSwap", (ev) => {
        if (ev.target.id !== "camera-panes") return;
        // a fresh read replaces the staged edits, and the bar with them
        this.activate(this.current);
        this.updateCameraBar();
      });
      const staged = (ev) => {
        if (ev.target.closest("#camera-panes [data-camera-key]")) {
          this.updateCameraBar();
        }
      };
      this.$root.addEventListener("input", staged);
      this.$root.addEventListener("change", staged);
      this.$root.addEventListener("click", (ev) => {
        const restore = ev.target.closest("[data-restore]");
        if (restore) this.openRestoreReview(restore.dataset.restore);
      });
      this.reviewDialog().addEventListener("change", () => this.syncApply());
      // Esc must not hide the result of an apply in flight
      this.reviewDialog().addEventListener("cancel", (ev) => {
        if (this.review.busy) ev.preventDefault();
      });
      globalThis.addEventListener("beforeunload", (ev) => {
        if (this.cameraChanges().count > 0) {
          ev.preventDefault();
          ev.returnValue = "";
        }
      });
    },

    // toggles the .is-active pane and the .active nav item imperatively (no
    // per-element getters -- the csp build only sees bare @click="method" refs).
    activate(section) {
      this.current = section;
      this.$root.querySelectorAll("[data-pane]").forEach((p) => {
        p.classList.toggle("is-active", p.dataset.pane === section);
      });
      this.$root.querySelectorAll("[data-section-nav]").forEach((n) => {
        n.classList.toggle("active", n.dataset.sectionNav === section);
      });
      this.updateCameraBar();
      if (section === "camera-backups") this.loadBackups();
    },

    select(ev) {
      this.activate(ev.currentTarget.dataset.sectionNav);
    },

    // ---- camera edits: staged in the inputs, reviewed in a dialog ----

    cameraControls() {
      return Array.from(
        this.$root.querySelectorAll("#camera-panes [data-camera-key]")
      );
    },

    // the staged value of one control, comparable with its data-original.
    controlValue(el) {
      return el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
    },

    // true when the control differs from what the camera reported.
    controlChanged(el) {
      if (el.disabled) return false;
      if (el.dataset.secret === "1") {
        // passwords start empty: a non-empty input is a change unless it is
        // just the value the reveal button filled in.
        return el.value !== "" && el.value !== el.dataset.revealed;
      }
      const original =
        el.type === "checkbox"
          ? el.dataset.original === "1" ? "1" : "0"
          : el.dataset.original;
      return this.controlValue(el) !== original;
    },

    // {changes: {key: value}, controls: [el], count} for every edited control.
    cameraChanges() {
      const changes = {};
      const controls = [];
      this.cameraControls().forEach((el) => {
        const changed = this.controlChanged(el);
        el.classList.toggle("is-changed", changed);
        if (!changed) return;
        changes[el.dataset.cameraKey] = this.controlValue(el);
        controls.push(el);
      });
      return { changes, controls, count: controls.length };
    },

    updateCameraBar() {
      const bar = this.$root.querySelector("#camera-apply-bar");
      if (!bar) return;
      const { count } = this.cameraChanges();
      const here =
        this.current.startsWith("camera-") && this.current !== "camera-backups";
      bar.hidden = count === 0 || !here;
      const label = bar.querySelector("[data-camera-count]");
      if (label) label.textContent = count + (count === 1 ? " change" : " changes");
    },

    discardCameraChanges() {
      this.cameraControls().forEach((el) => {
        if (el.type === "checkbox") {
          el.checked = el.dataset.original === "1";
        } else {
          el.value = el.dataset.original;
        }
        delete el.dataset.revealed;
        if (el.dataset.secret === "1") el.type = "password";
      });
      this.updateCameraBar();
    },

    // text a person sees for a control's value (option label, On/Off, raw).
    displayValue(el, raw) {
      if (el.type === "checkbox") return raw === "1" ? "On" : "Off";
      if (el.tagName === "SELECT") {
        const opt = Array.from(el.options).find((o) => o.value === raw);
        return opt ? opt.textContent : raw;
      }
      return raw;
    },

    reviewDialog() {
      return this.$root.querySelector("#camera-review");
    },

    reviewPart(name) {
      return this.reviewDialog().querySelector(`[${name}]`);
    },

    setReviewText(name, text) {
      const el = this.reviewPart(name);
      el.textContent = text;
      el.hidden = text === "";
    },

    // resets the dialog to a clean state before it is filled.
    resetReview() {
      ["data-review-table", "data-review-wifi", "data-review-format",
        "data-review-pending", "data-review-sync", "data-review-loss",
        "data-review-progress", "data-review-result"].forEach((n) => {
        this.reviewPart(n).hidden = true;
      });
      this.reviewPart("data-confirm-format").checked = false;
      this.reviewPart("data-accept-loss").checked = false;
      this.reviewPart("data-review-rows").replaceChildren();
      this.reviewPart("data-review-cancel").textContent = "Cancel";
      this.reviewPart("data-review-cancel").disabled = false;
      this.review = { kind: "changes", changes: {}, name: "", busy: false, checking: false, done: false };
    },

    // fills the table, shows warnings, and asks the camera how many recordings
    // would be lost when a format key is staged.
    async openCameraReview() {
      const staged = this.cameraChanges();
      if (staged.count === 0) return;
      this.resetReview();
      this.review.changes = staged.changes;
      this.setReviewText("data-review-title", "Review changes");
      const rows = this.reviewPart("data-review-rows");
      staged.controls.forEach((el) => {
        const secret = el.dataset.secret === "1";
        this.appendReviewRow(rows, [
          el.dataset.cameraLabel,
          secret ? "" : this.displayValue(el, el.dataset.original),
          secret ? "changed" : this.displayValue(el, this.controlValue(el)),
        ]);
      });
      this.reviewPart("data-review-table").hidden = false;
      const keys = Object.keys(staged.changes);
      this.reviewPart("data-review-wifi").hidden = !keys.some(
        (k) => k.startsWith("Wifi.") || k.startsWith("Cloud.")
      );
      const formats = staged.controls.some((el) => el.dataset.formatsCard === "1");
      this.showReview();
      if (formats) await this.checkPending(true);
      this.syncApply();
    },

    appendReviewRow(rows, cells) {
      const tr = document.createElement("tr");
      cells.forEach((text) => {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      });
      rows.appendChild(tr);
    },

    // the differences between the camera's file now and a backup; null when
    // the request fails (the reason is shown in the dialog).
    async fetchRestorePreview(name) {
      let resp;
      try {
        resp = await fetch("/api/camera/backups/" + encodeURIComponent(name), {
          headers: { Accept: "application/json" },
        });
      } catch {
        return { error: "network error; please retry" };
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
        return { error: "" };
      }
      if (resp.status === 200 && data && Array.isArray(data.changes)) {
        return { changes: data.changes };
      }
      return { error: failureText(resp, data) };
    },

    async openRestoreReview(name) {
      this.resetReview();
      const review = this.review;
      review.kind = "restore";
      review.name = name;
      review.checking = true;
      this.setReviewText("data-review-title", "Restore " + name);
      this.setReviewText(
        "data-review-progress",
        "Restoring replaces the camera's settings with this backup. Restoring " +
          "a different time zone, daylight, GPS time sync or image setting makes " +
          "the camera format its card."
      );
      this.showReview();
      const preview = await this.fetchRestorePreview(name);
      if (this.review !== review) return;
      if (!preview.changes) {
        review.checking = false;
        review.done = true;
        const result = this.reviewPart("data-review-result");
        result.classList.add("is-error");
        result.textContent = preview.error;
        result.hidden = false;
        this.reviewPart("data-review-cancel").textContent = "Close";
        this.syncApply();
        return;
      }
      const rows = this.reviewPart("data-review-rows");
      preview.changes.forEach((c) => this.appendReviewRow(rows, [c.label, c.from, c.to]));
      this.reviewPart("data-review-table").hidden = preview.changes.length === 0;
      this.reviewPart("data-review-wifi").hidden = !preview.changes.some(
        (c) => c.key.startsWith("Wifi.") || c.key.startsWith("Cloud.")
      );
      const formats = preview.changes.some((c) => {
        const el = this.$root.querySelector(
          `#camera-panes [data-camera-key="${CSS.escape(c.key)}"]`
        );
        return el && el.dataset.formatsCard === "1";
      });
      // a restore can change format keys, so the loss check always runs
      await this.checkPending(formats);
      this.syncApply();
    },

    showReview() {
      const dialog = this.reviewDialog();
      if (!dialog.open) dialog.showModal();
      this.syncApply();
    },

    closeCameraReview() {
      if (this.review && this.review.busy) return;
      this.reviewDialog().close();
    },

    // asks the camera how many recordings a format change would erase, with
    // Apply held back until it answers. force reveals the erase block even
    // when nothing is pending; otherwise it appears only when needed. a check
    // that finishes after the dialog was reset or reopened changes nothing.
    async checkPending(force) {
      const review = this.review;
      review.checking = true;
      this.syncApply();
      try {
        await this.fetchPending(force, review);
      } finally {
        review.checking = false;
        if (this.review === review) this.syncApply();
      }
    },

    async fetchPending(force, review) {
      if (force) this.reviewPart("data-review-format").hidden = false;
      let pending = null;
      try {
        const resp = await fetch("/api/camera/pending", {
          headers: { Accept: "application/json" },
        });
        const data = await readJson(resp);
        if (isAuthFailure(resp, data)) {
          redirectToLogin();
          return;
        }
        if (resp.status === 200 && data) pending = Number(data.pending);
      } catch {
        pending = null;
      }
      if (this.review !== review) return;
      if (pending === null) {
        this.reviewPart("data-review-format").hidden = false;
        this.setReviewText(
          "data-review-pending",
          "Can't check how many recordings are not downloaded yet: the camera " +
            "is not reachable right now."
        );
        return;
      }
      if (pending > 0) {
        this.reviewPart("data-review-format").hidden = false;
        this.setReviewText(
          "data-review-pending",
          pending + (pending === 1 ? " recording has" : " recordings have") +
            " not been downloaded yet."
        );
        this.reviewPart("data-review-sync").hidden = false;
        this.setReviewText(
          "data-review-loss-text",
          "I accept losing " + pending + (pending === 1 ? " recording" : " recordings")
        );
        this.reviewPart("data-review-loss").hidden = false;
      }
    },

    // Apply needs every visible confirmation ticked.
    syncApply() {
      const dialog = this.reviewDialog();
      const apply = this.reviewPart("data-review-apply");
      const format = this.reviewPart("data-review-format");
      const loss = this.reviewPart("data-review-loss");
      let ok = !this.review.busy && !this.review.checking && !this.review.done;
      if (!format.hidden && !this.reviewPart("data-confirm-format").checked) ok = false;
      if (!loss.hidden && !this.reviewPart("data-accept-loss").checked) ok = false;
      apply.disabled = !ok;
      apply.hidden = this.review.done;
      dialog.dataset.state = this.review.busy ? "busy" : this.review.done ? "done" : "ready";
    },

    resultText(data) {
      const parts = [];
      if (data.upload_confirmed === false) {
        parts.push(
          "The camera did not acknowledge the upload; the result below is " +
            "what it reports now."
        );
      }
      if (data.verified) {
        parts.push("Applied and verified (backup " + data.backup + ").");
      } else if (data.came_back) {
        parts.push("Applied, but the camera's settings could not be verified (backup " + data.backup + ").");
      } else {
        parts.push("Applied, but the camera has not come back yet (backup " + data.backup + ").");
        const wifi = (data.changed || []).some(
          (k) => k.startsWith("Wifi.") || k.startsWith("Cloud.")
        );
        if (wifi) {
          parts.push(
            "Wi-Fi or cloud settings changed, so it may now be on a different " +
              "network: check with the BlackVue app."
          );
        }
      }
      if (data.kept && data.kept.length > 0) {
        parts.push("The camera kept its own value for: " + data.kept.join(", ") + ".");
      }
      return parts.join(" ");
    },

    async applyCamera() {
      const review = this.review;
      const body = {
        confirm_format: this.reviewPart("data-confirm-format").checked === true,
        accept_loss: this.reviewPart("data-accept-loss").checked === true,
      };
      let path = "/api/camera/config";
      if (review.kind === "restore") {
        path = "/api/camera/backups/" + encodeURIComponent(review.name) + "/restore";
      } else {
        body.changes = review.changes;
      }
      review.busy = true;
      this.reviewPart("data-review-result").hidden = true;
      this.reviewPart("data-review-cancel").disabled = true;
      this.setReviewText(
        "data-review-progress",
        "Backing up the config, uploading, waiting for the camera to restart, " +
          "then verifying. This can take a couple of minutes."
      );
      this.syncApply();
      const resp = await this.send(path, body, "POST");
      const data = resp ? await readJson(resp) : null;
      review.busy = false;
      this.reviewPart("data-review-cancel").disabled = false;
      this.setReviewText("data-review-progress", "");
      if (resp && isAuthFailure(resp, data)) {
        redirectToLogin();
        return;
      }
      const result = this.reviewPart("data-review-result");
      result.classList.remove("is-error");
      if (resp && resp.status === 200 && data) {
        review.done = true;
        result.textContent = this.resultText(data);
        this.reviewPart("data-review-cancel").textContent = "Close";
        htmx.ajax("GET", "/hx/camera/panes", "#camera-panes");
        if (this.current === "camera-backups") this.loadBackups();
      } else {
        result.classList.add("is-error");
        let text = resp ? failureText(resp, data) : "network error; please retry";
        if (data && Array.isArray(data.errors) && data.errors.length > 0) {
          text += ": " + data.errors.join("; ");
        }
        if (data && data.code === "UPLOAD_FAILED") {
          text += ". Press Refresh to see what the camera holds now";
        }
        if (data && data.code === "FORMAT_CONFIRMATION_REQUIRED") {
          // the server decides which changes erase the card; show the gate
          this.reviewPart("data-review-format").hidden = false;
          await this.checkPending(true);
        }
        result.textContent = text;
      }
      result.hidden = false;
      this.syncApply();
    },

    // ---- backups pane ----

    async loadBackups() {
      const list = this.$root.querySelector("[data-backups-list]");
      const error = this.$root.querySelector("[data-backups-error]");
      const empty = this.$root.querySelector("[data-backups-empty]");
      let resp;
      try {
        resp = await fetch("/api/camera/backups", {
          headers: { Accept: "application/json" },
        });
      } catch {
        resp = null;
      }
      const data = resp ? await readJson(resp) : null;
      if (resp && isAuthFailure(resp, data)) {
        redirectToLogin();
        return;
      }
      list.replaceChildren();
      if (!resp || resp.status !== 200 || !Array.isArray(data)) {
        error.textContent = "could not list backups (" +
          (resp ? failureText(resp, data) : "network error") + ")";
        error.hidden = false;
        empty.hidden = true;
        return;
      }
      error.hidden = true;
      empty.hidden = data.length > 0;
      data.forEach((b) => {
        const li = document.createElement("li");
        li.className = "camera-backup";
        const name = document.createElement("span");
        name.className = "camera-backup-name";
        name.textContent = b.name;
        const when = document.createElement("span");
        when.className = "camera-backup-meta";
        when.textContent =
          new Date(b.created).toLocaleString() + " · " + this.sizeText(b.size);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button-secondary button-sm";
        button.dataset.restore = b.name;
        button.textContent = "Restore";
        li.append(name, when, button);
        list.appendChild(li);
      });
    },

    sizeText(bytes) {
      return bytes >= 1024 ? (bytes / 1024).toFixed(1) + " KiB" : bytes + " B";
    },

    // shows or re-masks one camera password; the value is fetched per click
    // and dropped again when hidden.
    async toggleSecret(button) {
      const key = button.dataset.reveal;
      const target = this.$root.querySelector(`[data-secret-value="${key}"]`);
      if (!target) return;
      if (button.getAttribute("aria-pressed") === "true") {
        target.textContent = MASK;
        const field = this.$root.querySelector(
          `#camera-panes [data-camera-key="${CSS.escape(key)}"]`
        );
        if (field) {
          field.type = "password";
          // the revealed value is dropped again unless the user edited it
          if (field.dataset.revealed !== undefined) {
            if (field.value === field.dataset.revealed) field.value = "";
            delete field.dataset.revealed;
          }
          this.updateCameraBar();
        }
        button.setAttribute("aria-pressed", "false");
        button.setAttribute("aria-label", "Show password");
        button.title = "Show password";
        return;
      }
      let resp;
      try {
        resp = await fetch("/api/camera/secret?key=" + encodeURIComponent(key), {
          headers: { Accept: "application/json" },
        });
      } catch {
        target.textContent = "can't be shown right now";
        return;
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
        return;
      }
      if (resp.status !== 200 || !data) {
        target.textContent = data?.error || "can't be shown";
        return;
      }
      target.textContent = data.value;
      // an editable password input is filled too; the revealed value is not a
      // change until the user alters it.
      const input = this.$root.querySelector(
        `#camera-panes [data-camera-key="${CSS.escape(key)}"]`
      );
      if (input && !input.disabled) {
        input.value = data.value;
        input.dataset.revealed = data.value;
        input.type = "text";
        this.updateCameraBar();
      }
      button.setAttribute("aria-pressed", "true");
      button.setAttribute("aria-label", "Hide password");
      button.title = "Hide password";
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
