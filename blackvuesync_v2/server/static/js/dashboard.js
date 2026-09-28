// dashboard.js: Alpine.js component for phase 2c active mode. owns the SSE
// connection, the sidebar controls, and the body[data-state] machine. all
// visibility is CSS-driven off data-state; this only mutates the attribute and
// feeds the hero its reactive progress snapshot.

const SSE_BACKOFF_START_MS = 2000;
const SSE_BACKOFF_MAX_MS = 30000;
const COMPLETE_LINGER_MS = 10000; // matches publisher POST_COMPLETE_RETENTION

function csrfToken() {
  const el = document.querySelector('meta[name="csrf-token"]');
  return el ? el.content : "";
}

function redirectToLogin() {
  location.assign("/login?next=" + encodeURIComponent(location.pathname));
}

// true when the session expired: 401 json, or a followed redirect to /login.
function isAuthFailure(resp) {
  return (
    resp.status === 401 ||
    (resp.redirected && new URL(resp.url).pathname === "/login")
  );
}

// the json "error" field when present, else the HTTP status.
async function errorText(resp) {
  const type = resp.headers.get("Content-Type") || "";
  if (type.includes("application/json")) {
    try {
      const body = await resp.json();
      if (body && body.error) return String(body.error);
    } catch {
      /* malformed body; fall back to the status */
    }
  }
  return "HTTP " + resp.status;
}

document.addEventListener("alpine:init", () => {
  Alpine.data("dashboardSync", () => ({
    progress: {
      state: "idle",
      percent: 0,
      files_completed: 0,
      files_total: 0,
      files_failed: 0,
      files_skipped: 0,
      current_file: null,
    },
    paused: false,
    _source: null,
    _backoffMs: SSE_BACKOFF_START_MS,
    _reconnectTimer: null,
    _lastMonotonic: -1,

    // --- csp-safe getters: directives may only reference a property or method ---

    get isRunning() {
      return this.progress.state === "running";
    },

    get badgeClass() {
      return "badge-" + this.progress.state;
    },

    get heroBarStyle() {
      return "width: " + (this.progress.percent || 0) + "%";
    },

    get hasFailures() {
      return this.progress.files_failed > 0;
    },

    get hasCurrentFile() {
      return Boolean(this.progress.current_file);
    },

    get currentFilename() {
      return this.progress.current_file
        ? this.progress.current_file.filename
        : "";
    },

    init() {
      this.progress.state = document.body.dataset.state || "idle";
      // reads paused flag from server-rendered data attribute on the root element.
      const root = this.$el;
      this.paused = root.dataset.paused === "true";
      // always open the stream so externally-started syncs are also detected.
      this.openStream();
    },

    // single writer of body[data-state]; css does the rest
    setState(state) {
      if (state === "running") {
        document.body.dataset.state = "running";
      } else if (state === "complete" || state === "failed") {
        document.body.dataset.state = "complete";
      } else {
        document.body.dataset.state = "idle";
      }
    },

    async syncNow() {
      const resp = await this.post("/api/sync/now", "Sync could not start");
      if (resp && (resp.status === 202 || resp.status === 409)) {
        this.setState("running");
        this.openStream();
      }
    },

    showActionError(message) {
      const el = this.$refs.actionError;
      if (!el) return;
      el.textContent = message;
      el.hidden = !message;
    },

    confirmStop() {
      this.$refs.stopDialog.showModal();
    },
    cancelStop() {
      this.$refs.stopDialog.close();
    },
    async doStop() {
      this.$refs.stopDialog.close();
      await this.post("/api/sync/stop", "Stop failed"); // SSE reports the terminal state
    },

    async togglePause() {
      const path = this.paused ? "/api/schedule/resume" : "/api/schedule/pause";
      const resp = await this.post(
        path,
        this.paused ? "Resume failed" : "Pause failed"
      );
      if (resp?.ok) {
        location.assign(location.pathname); // reflect the new Pause/Resume label
      }
    },

    // schedules the idle revert after a complete/failed linger period.
    _revertToIdle() {
      if (!this._source) {
        this.setState("idle");
        this.openStream(); // reopen to detect future syncs
      }
    },

    // schedules a reconnect attempt after the current backoff interval.
    _scheduleReconnect() {
      this.openStream(); // reconnect regardless of state to detect any sync
    },

    openStream() {
      if (this._source) return;
      const es = new EventSource("/api/sync/progress/stream");
      this._source = es;
      es.addEventListener("progress", (ev) => {
        this._backoffMs = SSE_BACKOFF_START_MS; // healthy frame resets backoff
        let snap;
        try {
          snap = JSON.parse(ev.data);
        } catch {
          /* malformed frame; the next event recovers */
          return;
        }
        if (snap.last_event_monotonic <= this._lastMonotonic) return; // stale
        this._lastMonotonic = snap.last_event_monotonic;
        this.progress = snap;
        this.setState(snap.state);
        if (snap.state === "complete" || snap.state === "failed") {
          this.closeStream();
          setTimeout(this._revertToIdle.bind(this), COMPLETE_LINGER_MS);
        }
      });
      es.onerror = () => {
        this.closeStream();
        this._reconnectTimer = setTimeout(
          this._scheduleReconnect.bind(this),
          this._backoffMs
        );
        this._backoffMs = Math.min(this._backoffMs * 2, SSE_BACKOFF_MAX_MS);
      };
    },

    closeStream() {
      if (this._source) {
        this._source.close();
        this._source = null;
      }
      if (this._reconnectTimer) {
        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = null;
      }
    },

    // posts to path; returns the response, or null after surfacing a network
    // error. non-2xx responses (except 409 "already running") are shown as
    // "<label>: <reason>"; an expired session navigates to /login.
    async post(path, label) {
      let resp;
      try {
        resp = await fetch(path, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken() },
        });
      } catch {
        this.showActionError(label + ": network error");
        return null;
      }
      if (isAuthFailure(resp)) {
        redirectToLogin();
        return null;
      }
      if (resp.ok || resp.status === 409) {
        this.showActionError("");
      } else {
        this.showActionError(label + ": " + (await errorText(resp)));
      }
      return resp;
    },
  }));
});

// unified 302 -> /login for htmx-driven idle polls: flask login_required
// redirects to /login; htmx would otherwise swap the login page into a card.
document.body.addEventListener("htmx:beforeSwap", (event) => {
  const xhr = event.detail.xhr;
  if (xhr?.responseURL?.includes("/login")) {
    event.detail.shouldSwap = false;
    location.assign("/login?next=" + encodeURIComponent(location.pathname));
  }
});
