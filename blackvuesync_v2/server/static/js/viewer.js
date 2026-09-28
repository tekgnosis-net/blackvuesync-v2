// viewer.js: plain-JS dashcam viewer. loads recordings, plays front+rear in
// lockstep (front master, rear slaved), and (part 2) drives a Leaflet map +
// Chart.js telemetry off video.currentTime, accumulating across an
// auto-advanced journey. csp-clean: no eval, no innerHTML for server data.

const KMH_PER_KNOT = 1.852;
const MPH_PER_KNOT = 1.15078;
const DRIFT_TOLERANCE = 0.15; // seconds before re-pinning the slave video
const RECORDINGS_API = "/api/viewer/recordings";
const DAYS_API = "/api/viewer/days";
const DEFAULT_SEGMENT_SECONDS = 60; // blackvue writes ~1-minute segments

function fmtTime(seconds) {
  const total = Math.floor(Number(seconds) || 0);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return mins + ":" + String(secs).padStart(2, "0");
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

// fetches json; returns { data } on success or { error } with a display message.
// an expired session navigates to /login instead.
async function fetchJson(url) {
  let resp;
  try {
    resp = await fetch(url, { headers: { Accept: "application/json" } });
  } catch {
    return { error: "network error" };
  }
  if (isAuthFailure(resp)) {
    redirectToLogin();
    return { error: "session expired" };
  }
  if (!resp.ok) {
    return { error: "HTTP " + resp.status };
  }
  const type = resp.headers.get("Content-Type") || "";
  if (!type.includes("application/json")) {
    return { error: "unexpected response" };
  }
  try {
    return { data: await resp.json() };
  } catch {
    return { error: "malformed response" };
  }
}

function recordingKey(rec) {
  return rec.base_filename + "_" + rec.type;
}

const viewer = {
  el: null,
  front: null,
  rear: null,
  player: null,
  speedUnit: "kmh",
  journeyMode: "progressive",
  chain: [],
  index: 0,
  activeKey: null,
  _selectSeq: 0, // generation token: bumped per selection; stale awaits bail

  init() {
    this.el = document.getElementById("viewer-app");
    if (!this.el) return;
    this.front = document.getElementById("viewer-front");
    this.rear = document.getElementById("viewer-rear");
    this.player = this.el.querySelector(".viewer-player");
    this.speedUnit = this.el.dataset.speedUnit || "kmh";
    this.journeyMode = this.el.dataset.journeyMode || "progressive"; // consumed in part 2 (progressive vs full telemetry loading)
    this.bindTransport();
    this.bindSync();
    this.loadRecordings();
    this.initTelemetry();
  },

  showError(message) {
    const el = document.getElementById("viewer-error");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
  },

  clearError() {
    const el = document.getElementById("viewer-error");
    if (el) el.hidden = true;
  },

  sidebarNote(parent, text) {
    const note = document.createElement("p");
    note.className = "viewer-note";
    note.textContent = text;
    parent.replaceChildren(note);
  },

  // the sidebar lists days only; a day's recordings load when it is opened,
  // so a library of tens of thousands of recordings never renders at once.
  async loadRecordings() {
    const side = document.getElementById("viewer-recordings");
    this.sidebarNote(side, "Loading recordings…");
    const { data, error } = await fetchJson(DAYS_API);
    if (!data) {
      this.sidebarNote(side, "Could not load recordings (" + error + ").");
      return;
    }
    if (data.days.length === 0) {
      this.sidebarNote(side, "No recordings downloaded yet.");
      return;
    }
    side.replaceChildren(...data.days.map((day) => this.dayGroup(day)));
    this.toggleDay(side.querySelector(".viewer-day"));
  },

  dayGroup(day) {
    const group = document.createElement("div");
    group.className = "viewer-day";
    group.dataset.date = day.date;
    const header = document.createElement("button");
    header.type = "button";
    header.className = "viewer-day-label";
    header.setAttribute("aria-expanded", "false");
    const date = document.createElement("span");
    date.textContent = day.date;
    const count = document.createElement("span");
    count.className = "viewer-day-count";
    count.textContent = String(day.count);
    header.append(date, count);
    header.addEventListener("click", () => this.toggleDay(group));
    const body = document.createElement("div");
    body.className = "viewer-day-body";
    body.hidden = true;
    group.append(header, body);
    return group;
  },

  async toggleDay(group) {
    if (!group) return;
    const header = group.querySelector(".viewer-day-label");
    const body = group.querySelector(".viewer-day-body");
    const open = header.getAttribute("aria-expanded") !== "true";
    header.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    if (!open || group.dataset.loaded) return;
    group.dataset.loaded = "loading";
    this.sidebarNote(body, "Loading…");
    const url = RECORDINGS_API + "?date=" + encodeURIComponent(group.dataset.date);
    const { data, error } = await fetchJson(url);
    if (!data) {
      delete group.dataset.loaded; // retried on the next open
      this.sidebarNote(body, "Could not load this day (" + error + ").");
      return;
    }
    group.dataset.loaded = "done";
    const recs = data.days.flatMap((day) => day.recordings);
    body.replaceChildren(...recs.map((rec) => this.recRow(rec)));
    if (this.activeKey) this.markActive(this.activeKey);
  },

  recRow(rec) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "viewer-rec";
    row.dataset.key = recordingKey(rec);
    if (rec.thumb) {
      const img = document.createElement("img");
      img.loading = "lazy"; // only thumbnails scrolled into view are fetched
      img.decoding = "async";
      img.src = rec.thumb;
      img.alt = "";
      row.append(img);
    }
    const time = document.createElement("span");
    time.textContent = rec.datetime.slice(11, 16);
    const badge = document.createElement("span");
    badge.className = "viewer-badge";
    badge.textContent = rec.type;
    row.append(time, badge);
    row.addEventListener("click", () => this.selectRecording(rec));
    return row;
  },

  markActive(key) {
    this.activeKey = key;
    this.el.querySelectorAll(".viewer-rec").forEach((row) => {
      row.classList.toggle("active", row.dataset.key === key);
    });
  },

  async selectRecording(rec) {
    const seq = (this._selectSeq = this._selectSeq + 1);
    this.markActive(recordingKey(rec));
    this.clearError();
    const journey = await fetchJson(
      RECORDINGS_API + "/" + recordingKey(rec) + "/journey"
    );
    if (seq !== this._selectSeq) return; // a newer selection superseded this one
    if (journey.error) {
      this.showError("Could not load the journey (" + journey.error + ").");
    }
    this.chain = journey.data?.segments ?? [rec];
    this.index = 0;
    this.resetTelemetry();
    await this.loadSegment(0, true);
    if (seq !== this._selectSeq) return;
    if (this.journeyMode === "full") {
      this.prefetchRest(0);
    }
  },

  async loadSegment(i, autoplay) {
    const seg = this.chain[i];
    if (!seg) return;
    const seq = this._selectSeq;
    this.index = i;
    // front prefers F; the rear slot shows another direction (R first), never
    // the same file as the front.
    const frontDir = seg.videos.F ? "F" : seg.directions.find((d) => seg.videos[d]);
    const rearDir = ["R", ...seg.directions].find(
      (d) => d !== frontDir && seg.videos[d]
    );
    this.front.src = seg.videos[frontDir];
    if (rearDir) {
      this.rear.src = seg.videos[rearDir];
      this.rear.style.display = "";
    } else {
      this.rear.pause();
      this.rear.removeAttribute("src");
      this.rear.load(); // releases the previous rear stream
      this.rear.style.display = "none";
    }
    await this.loadSegmentTelemetry(seg, i);
    if (seq !== this._selectSeq) return; // superseded while telemetry loaded
    if (autoplay) {
      this.front.play().catch(() => {
        // autoplay may be blocked until a user gesture; ignore
      });
    }
  },

  bindSync() {
    const sync = () => {
      if (!this.rear.src) return;
      if (Math.abs(this.rear.currentTime - this.front.currentTime) > DRIFT_TOLERANCE) {
        this.rear.currentTime = this.front.currentTime;
      }
    };
    this.front.addEventListener("play", () => {
      if (this.rear.src) this.rear.play().catch(() => { /* slave play blocked; ignore */ });
    });
    this.front.addEventListener("pause", () => this.rear.pause());
    this.front.addEventListener("seeking", sync);
    this.front.addEventListener("ratechange", () => {
      this.rear.playbackRate = this.front.playbackRate;
    });
    this.front.addEventListener("timeupdate", () => {
      this.updateTimeUi();
      sync();
      this.onTick();
    });
    this.front.addEventListener("ended", () => this.onSegmentEnded());
    this.front.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(this.front.duration) && this.front.duration > 0) {
        this.durations[this.index] = this.front.duration;
      }
    });
  },

  bindTransport() {
    document.getElementById("viewer-play").addEventListener("click", () => {
      if (this.front.paused) this.front.play().catch(() => { /* ignore */ });
      else this.front.pause();
    });
    document.getElementById("viewer-seek").addEventListener("input", (ev) => {
      const frac = Number(ev.currentTarget.value) / 1000;
      if (this.front.duration) this.front.currentTime = frac * this.front.duration;
    });
    document.getElementById("viewer-layout").addEventListener("click", () => {
      const pip = this.player.dataset.layout === "pip";
      this.player.dataset.layout = pip ? "sbs" : "pip";
    });
    document.getElementById("viewer-swap").addEventListener("click", () => {
      this.front.classList.toggle("viewer-video-primary");
      this.front.classList.toggle("viewer-video-secondary");
      this.rear.classList.toggle("viewer-video-primary");
      this.rear.classList.toggle("viewer-video-secondary");
    });
    document.getElementById("viewer-next").addEventListener("click", () => this.onSegmentEnded());
  },

  updateTimeUi() {
    const seek = document.getElementById("viewer-seek");
    if (this.front.duration) {
      seek.value = String(Math.round((this.front.currentTime / this.front.duration) * 1000));
    }
    document.getElementById("viewer-time").textContent =
      fmtTime(this.front.currentTime) + " / " + fmtTime(this.front.duration);
  },

  // --- telemetry state (part 2) ---
  map: null,
  pathLayer: null,
  marker: null,
  gsChart: null,
  track: [], // accumulated {seg, t: segment-local s, lat, lon, speed} across the journey
  gforce: [], // accumulated {seg, t, mag} for the g-sensor chart
  durations: [], // per-segment video duration (s) from loadedmetadata
  spans: [], // per-segment telemetry span (s); fallback when the video is not loaded
  loaded: null, // Set of chain indices whose telemetry has been appended (idempotency)

  initTelemetry() {
    const leaflet = globalThis.L;
    this.map = leaflet.map("viewer-map");
    leaflet
      .tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap",
      })
      .addTo(this.map);
    this.map.setView([0, 0], 2);
    this.gsChart = new globalThis.Chart(document.getElementById("viewer-gsensor"), {
      type: "line",
      data: { labels: [], datasets: [{ label: "G", data: [], pointRadius: 0, borderColor: "#ff9f0a" }] },
      options: {
        responsive: true,
        animation: false,
        plugins: { legend: { display: false } },
        scales: { x: { display: false } },
      },
    });
  },

  resetTelemetry() {
    this.track = [];
    this.gforce = [];
    this.durations = [];
    this.spans = [];
    this.loaded = new Set();
    if (this.pathLayer) {
      this.pathLayer.remove();
      this.pathLayer = null;
    }
    if (this.marker) {
      this.marker.remove();
      this.marker = null;
    }
  },

  segmentDuration(k) {
    // video duration, else telemetry span, else the nominal segment length
    return this.durations[k] || this.spans[k] || DEFAULT_SEGMENT_SECONDS;
  },

  segmentOffset(i) {
    // cumulative session time (s) at the start of segment i
    let total = 0;
    for (let k = 0; k < i; k += 1) {
      total += this.segmentDuration(k);
    }
    return total;
  },

  sessionTime(p) {
    return this.segmentOffset(p.seg) + p.t;
  },

  async loadSegmentTelemetry(seg, i) {
    if (this.loaded.has(i)) {
      return; // this segment's telemetry is already accumulated
    }
    this.loaded.add(i);
    const seq = this._selectSeq;
    const key = recordingKey(seg);
    let span = 0;
    const failed = [];
    if (seg.has_gps) {
      const gps = await fetchJson(RECORDINGS_API + "/" + key + "/gps");
      if (seq !== this._selectSeq) return; // arrays now belong to another recording
      if (gps.error) failed.push("GPS " + gps.error);
      for (const p of gps.data?.points ?? []) {
        this.track.push({ seg: i, t: p.t, lat: p.lat, lon: p.lon, speed: p.speed });
        span = Math.max(span, p.t);
      }
    }
    if (seg.has_3gf) {
      const gs = await fetchJson(RECORDINGS_API + "/" + key + "/gsensor");
      if (seq !== this._selectSeq) return;
      if (gs.error) failed.push("G-sensor " + gs.error);
      for (const s of gs.data?.samples ?? []) {
        this.gforce.push({ seg: i, t: s.t, mag: Math.hypot(s.x, s.y, s.z) });
        span = Math.max(span, s.t);
      }
    }
    if (failed.length) {
      this.showError("Could not load telemetry: " + failed.join(", ") + ".");
    }
    this.spans[i] = span;
    this.redrawTrack();
  },

  async prefetchRest(fromIndex) {
    // full mode: load the remaining chain's telemetry up front (each call is
    // idempotent via the `loaded` set, so this never double-appends).
    const seq = this._selectSeq;
    for (let i = fromIndex + 1; i < this.chain.length; i += 1) {
      if (seq !== this._selectSeq) return;
      await this.loadSegmentTelemetry(this.chain[i], i);
    }
  },

  redrawTrack() {
    const leaflet = globalThis.L;
    // segments may load out of order (prefetch vs skip); order on the journey timeline
    const byTime = (a, b) => this.sessionTime(a) - this.sessionTime(b);
    this.track.sort(byTime);
    this.gforce.sort(byTime);
    const latlngs = this.track.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
    if (latlngs.length) {
      if (this.pathLayer) {
        this.pathLayer.remove();
      }
      this.pathLayer = leaflet.polyline(latlngs, { color: "#0a84ff", weight: 3 }).addTo(this.map);
      this.map.fitBounds(this.pathLayer.getBounds(), { padding: [20, 20] });
      if (!this.marker) {
        this.marker = leaflet
          .circleMarker(latlngs[0], { radius: 6, color: "#fff", fillColor: "#0a84ff", fillOpacity: 1 })
          .addTo(this.map);
      }
    }
    this.gsChart.data.labels = this.gforce.map(() => "");
    this.gsChart.data.datasets[0].data = this.gforce.map((g) => g.mag);
    this.gsChart.update("none");
  },

  nearest(seg, t) {
    // nearest track point of segment `seg` to its local time t (linear scan;
    // tracks are small). other segments are ignored so the marker never jumps.
    let best = null;
    let bestDelta = Infinity;
    for (const p of this.track) {
      if (p.seg !== seg) continue;
      const delta = Math.abs(p.t - t);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = p;
      }
    }
    return best;
  },

  onTick() {
    const point = this.nearest(this.index, this.front.currentTime);
    if (point && this.marker) {
      this.marker.setLatLng([point.lat, point.lon]);
    }
    const speedEl = document.getElementById("viewer-speed-value");
    if (point && point.speed != null) {
      const factor = this.speedUnit === "mph" ? MPH_PER_KNOT : KMH_PER_KNOT;
      speedEl.textContent = String(Math.round(point.speed * factor));
    } else {
      speedEl.textContent = "--";
    }
  },

  onSegmentEnded() {
    const next = this.index + 1;
    if (next < this.chain.length) {
      this.loadSegment(next, true);
    }
  },
};

document.addEventListener("DOMContentLoaded", () => viewer.init());
