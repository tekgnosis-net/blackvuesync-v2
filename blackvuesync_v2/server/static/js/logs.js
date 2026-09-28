// logs.js: Alpine.js (csp build) component for the live /logs viewer. owns the
// SSE connection to /api/logs/stream, client-side level + text filtering, tail
// ergonomics (pause auto-scroll, clear), and the live verbosity control.
//
// log rows are built with textContent only -- never innerHTML -- because log
// messages are untrusted text. streamed lines are de-duplicated against the
// server-rendered snapshot and across reconnects via a monotonic seq watermark:
// the stream replays the current buffer on connect, so a line already shown
// (seq <= _lastSeq) is skipped. seq restarts with every server process, so a
// frame carrying a different boot_id (or a seq behind the watermark) resets it.

const SSE_BACKOFF_START_MS = 2000;
const SSE_BACKOFF_MAX_MS = 30000;
const LOGIN_URL = "/login?next=/logs";
const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
// verbosity token -> the settings patch body it maps to (avoids a nested ternary).
const VERBOSITY_BODY = {
  quiet: { quiet: true, verbose: 0 },
  normal: { quiet: false, verbose: 0 },
  verbose: { quiet: false, verbose: 1 },
  debug: { quiet: false, verbose: 2 },
};

function csrfToken() {
  const el = document.querySelector('meta[name="csrf-token"]');
  return el ? el.content : "";
}

document.addEventListener("alpine:init", () => {
  Alpine.data("logsPage", () => ({
    minLevelNo: 0,
    query: "",
    paused: false,
    verbosity: "normal",
    capacity: 1000,
    _lastSeq: 0,
    _bootId: "",
    error: "",
    _source: null,
    _backoffMs: SSE_BACKOFF_START_MS,
    _reconnectTimer: null,
    _pane: null,

    get pauseLabel() {
      return this.paused ? "Resume" : "Pause";
    },

    init() {
      this.verbosity = this.$el.dataset.verbosity || "normal";
      const cap = Number.parseInt(this.$el.dataset.capacity || "1000", 10);
      this.capacity = Number.isFinite(cap) && cap > 0 ? cap : 1000;
      this._pane = this.$el.querySelector("[data-pane]");
      this._bootId = this.$el.dataset.bootId || "";
      this._lastSeq = this.maxRenderedSeq();
      this.highlightVerbosity();
      this.scrollToEnd();
      this.openStream();
    },

    // highest seq among the server-rendered rows; 0 when the pane is empty.
    maxRenderedSeq() {
      let max = 0;
      this._pane.querySelectorAll("[data-seq]").forEach((row) => {
        const seq = Number.parseInt(row.dataset.seq || "0", 10);
        if (Number.isFinite(seq) && seq > max) max = seq;
      });
      return max;
    },

    // --- controls ---
    setLevel(ev) {
      const level = ev.currentTarget.dataset.level;
      this.minLevelNo = LEVEL_ORDER[level] || 0;
      this.$root.querySelectorAll(".logs-level-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.level === level);
      });
      this.applyFilters();
    },

    onSearch(ev) {
      this.query = (ev.currentTarget.value || "").toLowerCase();
      this.applyFilters();
    },

    togglePause() {
      this.paused = !this.paused;
      if (!this.paused) this.scrollToEnd();
    },

    clearView() {
      while (this._pane.firstChild) this._pane.firstChild.remove();
      this.updateEmpty();
    },

    async setVerbosity(ev) {
      const token = ev.currentTarget.dataset.verbosity;
      const body = VERBOSITY_BODY[token];
      if (!body) return;
      const resp = await this.send("/api/settings/logging", body, "PATCH");
      if (!resp) {
        this.error = "Could not reach the server; verbosity unchanged.";
        return;
      }
      if (resp.status === 401) {
        globalThis.location.assign(LOGIN_URL);
        return;
      }
      if (!resp.ok) {
        this.error = "Could not change verbosity (HTTP " + resp.status + ").";
        return;
      }
      this.error = "";
      this.verbosity = token;
      this.highlightVerbosity();
    },

    // --- rendering ---
    // detects a server restart: a new boot id, or a frame whose newest line is
    // behind the watermark (the replayed buffer always reaches the newest line).
    syncBoot(bootId, lines) {
      const maxSeq = lines.reduce((m, ln) => Math.max(m, ln.seq), 0);
      const newBoot = bootId && this._bootId && bootId !== this._bootId;
      const wentBack = lines.length > 0 && maxSeq < this._lastSeq;
      if (bootId) this._bootId = bootId;
      if (!newBoot && !wentBack) return;
      this._lastSeq = 0;
      this._pane.appendChild(this.markerRow("server restarted"));
    },

    markerRow(text) {
      const row = document.createElement("div");
      row.className = "log-row log-row-marker";
      row.dataset.levelNo = "100"; // passes every level filter
      row.append(this.cell("log-msg", "-- " + text + " --"));
      return row;
    },

    appendLines(lines) {
      // skip lines already shown (server-rendered snapshot or a prior frame):
      // the stream replays the buffer on connect, so seq is the dedup key.
      const fresh = lines.filter((ln) => ln.seq > this._lastSeq);
      if (fresh.length === 0) return;
      const atEnd = this.isScrolledToEnd();
      const frag = document.createDocumentFragment();
      fresh.forEach((ln) => {
        frag.appendChild(this.buildRow(ln));
        if (ln.seq > this._lastSeq) this._lastSeq = ln.seq;
      });
      this._pane.appendChild(frag);
      this.trimToCapacity();
      this.updateEmpty();
      if (!this.paused && atEnd) this.scrollToEnd();
    },

    buildRow(ln) {
      const row = document.createElement("div");
      row.className = "log-row";
      row.dataset.level = ln.level;
      row.dataset.levelNo = String(ln.level_no);
      row.dataset.seq = String(ln.seq);
      row.append(
        this.cell("log-ts", ln.ts),
        this.cell("log-level log-level-" + ln.level, ln.level),
        this.cell("log-logger", ln.logger),
        this.cell("log-msg", ln.message)
      );
      this.applyRowVisibility(row);
      return row;
    },

    // builds one row cell with textContent (never innerHTML; messages are untrusted).
    cell(className, text) {
      const span = document.createElement("span");
      span.className = className;
      span.textContent = text;
      return span;
    },

    applyRowVisibility(row) {
      const levelNo = Number.parseInt(row.dataset.levelNo || "0", 10);
      const text = row.textContent.toLowerCase();
      const visible =
        levelNo >= this.minLevelNo && (!this.query || text.includes(this.query));
      row.classList.toggle("hidden", !visible);
    },

    applyFilters() {
      this._pane
        .querySelectorAll(".log-row")
        .forEach((row) => this.applyRowVisibility(row));
    },

    trimToCapacity() {
      let extra = this._pane.childElementCount - this.capacity;
      while (extra-- > 0 && this._pane.firstChild) {
        this._pane.firstChild.remove();
      }
    },

    updateEmpty() {
      const empty = this.$root.querySelector("[data-empty]");
      if (empty) empty.hidden = this._pane.childElementCount > 0;
    },

    highlightVerbosity() {
      this.$root.querySelectorAll("[data-verbosity]").forEach((btn) => {
        if (btn.dataset.verbosity) {
          btn.classList.toggle("active", btn.dataset.verbosity === this.verbosity);
        }
      });
    },

    isScrolledToEnd() {
      const pane = this._pane;
      return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
    },

    scrollToEnd() {
      this._pane.scrollTop = this._pane.scrollHeight;
    },

    // --- SSE lifecycle (mirrors dashboard.js) ---
    openStream() {
      if (this._source) return;
      const es = new EventSource("/api/logs/stream");
      this._source = es;
      es.addEventListener("logs", (ev) => this.onFrame(ev));
      es.onerror = () => this.onStreamError();
    },

    onFrame(ev) {
      this._backoffMs = SSE_BACKOFF_START_MS; // healthy frame resets backoff
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        /* malformed frame; the next event recovers */
        return;
      }
      const lines = data?.lines;
      if (!Array.isArray(lines)) return;
      this.syncBoot(data.boot_id || "", lines);
      this.appendLines(lines);
    },

    onStreamError() {
      this.closeStream();
      this.checkAuth();
      this._reconnectTimer = setTimeout(() => this.openStream(), this._backoffMs);
      this._backoffMs = Math.min(this._backoffMs * 2, SSE_BACKOFF_MAX_MS);
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

    // EventSource hides the http status, so a failed stream probes the session
    // and sends an expired one to the login page instead of retrying forever.
    async checkAuth() {
      try {
        const resp = await fetch("/api/auth/me", { headers: { Accept: "application/json" } });
        if (resp.status === 401) globalThis.location.assign(LOGIN_URL);
      } catch {
        /* server unreachable; the reconnect backoff keeps trying */
      }
    },

    async send(path, body, method) {
      try {
        return await fetch(path, {
          method: method,
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          body: JSON.stringify(body),
        });
      } catch {
        /* network error; caller guards against null */
        return null;
      }
    },
  }));
});
