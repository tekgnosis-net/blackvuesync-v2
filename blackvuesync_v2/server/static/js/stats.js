// stats.js: Alpine.js (csp build) component for the /stats page. fetches
// /api/stats/series on load and on range change, then renders vendored
// Chart.js charts (incl. the disk actual + projected + limit datasets).
// csp-clean: no eval, no inline expressions; values rendered via textContent.

const RANGE_DEFAULT = "7d";
const FAILURE_REASONS = ["http", "network", "timeout", "disk", "unknown"];
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];

function fmtBytes(n) {
  let value = Number(n) || 0;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return value.toFixed(unit === 0 ? 0 : 1) + " " + BYTE_UNITS[unit];
}

function tsLabel(ts) {
  return new Date(Number(ts) * 1000).toLocaleString();
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

function failureDatasets(points) {
  return FAILURE_REASONS.map((reason) => ({
    label: reason,
    data: points.map((p) => p.failures?.[reason] ?? 0),
  }));
}

document.addEventListener("alpine:init", () => {
  Alpine.data("statsPage", () => ({
    range: RANGE_DEFAULT,
    charts: {},

    init() {
      this.range = this.$el.dataset.initialRange || RANGE_DEFAULT;
      this.load();
    },

    setRange(ev) {
      this.range = ev.currentTarget.dataset.range;
      this.$root.querySelectorAll("[data-range]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.range === this.range);
      });
      this.load();
    },

    async load() {
      const { data, error } = await this.fetchSeries(this.range);
      if (!data) {
        // charts keep their last state; the banner says why they are stale
        this.showError(error ? "Could not load statistics (" + error + ")." : "");
        return;
      }
      this.showError("");
      this.renderSummary(data.summary);
      this.renderCharts(data);
    },

    showError(message) {
      const el = this.$root.querySelector("[data-stats-error]");
      if (!el) return;
      el.textContent = message;
      el.hidden = !message;
    },

    // returns { data } on success, { error } on failure, or {} after
    // navigating to /login on an expired session.
    async fetchSeries(range) {
      let resp;
      try {
        resp = await fetch(
          "/api/stats/series?range=" + encodeURIComponent(range),
          { headers: { Accept: "application/json" } },
        );
      } catch {
        return { error: "network error" };
      }
      if (isAuthFailure(resp)) {
        redirectToLogin();
        return {};
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
    },

    renderSummary(summary) {
      this.setText("[data-summary-runs]", String(summary.runs));
      this.setText("[data-summary-bytes]", fmtBytes(summary.bytes));
      this.setText(
        "[data-summary-duration]",
        (summary.avg_duration_seconds || 0).toFixed(1) + " s",
      );
      this.setText(
        "[data-summary-success]",
        (summary.success_rate * 100).toFixed(1) + "%",
      );
    },

    setText(selector, text) {
      const el = this.$root.querySelector(selector);
      if (el) {
        el.textContent = text;
      }
    },

    renderCharts(data) {
      const points = data.series.points;
      const labels = points.map((p) => tsLabel(p.ts));
      // volume charts exclude dry-run rows (they download nothing)
      const volume = points.filter((p) => !p.dry_run);
      const volumeLabels = volume.map((p) => tsLabel(p.ts));
      this.drawLine("bytes", volumeLabels, volume.map((p) => p.bytes), "Bytes");
      this.drawBar("files", volumeLabels, volume.map((p) => p.files), "Files");
      this.drawLine("duration", labels, points.map((p) => p.duration), "Seconds");
      this.drawFailures("failures", labels, points);
      this.drawDisk("disk", points, data.forecast);
    },

    canvas(name) {
      return this.$root.querySelector('[data-chart="' + name + '"]');
    },

    upsert(name, config) {
      if (this.charts[name]) {
        this.charts[name].destroy();
      }
      this.charts[name] = new globalThis.Chart(this.canvas(name), config);
    },

    drawLine(name, labels, values, label) {
      this.upsert(name, {
        type: "line",
        data: { labels: labels, datasets: [{ label: label, data: values, tension: 0.3 }] },
        options: { responsive: true, plugins: { legend: { display: false } } },
      });
    },

    drawBar(name, labels, values, label) {
      this.upsert(name, {
        type: "bar",
        data: { labels: labels, datasets: [{ label: label, data: values }] },
        options: { responsive: true, plugins: { legend: { display: false } } },
      });
    },

    drawFailures(name, labels, points) {
      this.upsert(name, {
        type: "bar",
        data: { labels: labels, datasets: failureDatasets(points) },
        options: { responsive: true, scales: { x: { stacked: true }, y: { stacked: true } } },
      });
    },

    drawDisk(name, points, forecast) {
      const projected = forecast.projected || [];
      const labels = points.map((p) => tsLabel(p.ts)).concat(projected.map((p) => tsLabel(p.ts)));
      const nActual = points.length;
      const actualData = points.map((p) => p.disk).concat(projected.map(() => null));
      const projData = [];
      for (let i = 0; i < labels.length; i += 1) {
        if (nActual > 0 && i === nActual - 1) {
          projData.push(points[nActual - 1].disk); // anchor the dashed line to the last actual point
        } else if (i >= nActual) {
          projData.push(projected[i - nActual].disk);
        } else {
          projData.push(null);
        }
      }
      const datasets = [
        { label: "actual", data: actualData, borderColor: "#0a84ff", tension: 0.3 },
        { label: "projected", data: projData, borderColor: "#5e5ce6", borderDash: [6, 5], tension: 0.3 },
      ];
      const cap = forecast.limits?.max_used_disk_percent;
      const steady = forecast.limits?.keep_steady_state;
      if (cap !== null && cap !== undefined) {
        datasets.push(this.limitLine("max cap", cap, labels.length, "#ff453a"));
      }
      if (steady !== null && steady !== undefined) {
        datasets.push(this.limitLine("retention", steady, labels.length, "#ff9f0a"));
      }
      this.upsert(name, {
        type: "line",
        data: { labels: labels, datasets: datasets },
        options: {
          responsive: true,
          scales: {
            y: {
              min: 0,
              max: 1,
              ticks: { callback: (value) => Math.round(value * 100) + "%" },
            },
          },
        },
      });
    },

    limitLine(label, value, count, color) {
      return {
        label: label,
        data: new Array(count).fill(value),
        borderColor: color,
        borderDash: [4, 4],
        pointRadius: 0,
      };
    },
  }));
});
