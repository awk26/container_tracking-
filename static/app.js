const form = document.getElementById("track-form");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const lineSelect = document.getElementById("line");
const containerInput = document.getElementById("container");

lineSelect.addEventListener("change", () => {
  containerInput.value = "";
});

let activeMap = null;
let activeCharts = [];

const CHART_PALETTE = {
  text: "#e7ebee",
  muted: "#8b9aa5",
  border: "#2a333b",
  card: "#171d22",
  brand: "#4da3ff",
  ok: "#3ddc84",
  amber: "#ffb84d",
  series: ["#4da3ff", "#3ddc84", "#ffb84d", "#ff8a80", "#c792ea"],
};

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.href !== undefined) node.href = opts.href;
  if (opts.target !== undefined) node.target = opts.target;
  if (opts.rel !== undefined) node.rel = opts.rel;
  children.forEach((child) => node.appendChild(child));
  return node;
}

function setStatus(message, kind) {
  if (!message) {
    statusEl.hidden = true;
    statusEl.textContent = "";
    statusEl.className = "status";
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.className = `status ${kind}`;
}

function clearResult() {
  resultEl.hidden = true;
  resultEl.replaceChildren();
  if (activeMap) {
    activeMap.remove();
    activeMap = null;
  }
  activeCharts.forEach((chart) => chart.destroy());
  activeCharts = [];
  pendingChartRenders = [];
}

// Draws the route on a Leaflet map: a marker per point (the current one
// pulses), connected by a line, zoomed to fit. Requires the container to
// already be attached to the visible DOM with a real height.
function initRouteMap(container, route) {
  const points = route.points || [];
  if (!points.length) return;

  const map = L.map(container, { scrollWheelZoom: false });
  activeMap = map;

  // Plain OpenStreetMap tiles (no API key needed), darkened with a CSS
  // filter on the tile pane so the map matches the dark theme.
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
    className: "dark-tiles",
  }).addTo(map);

  const latLngs = points.map((p) => [p.lat, p.lon]);
  L.polyline(latLngs, { color: CHART_PALETTE.brand, weight: 3, opacity: 0.9 }).addTo(map);

  points.forEach((point, i) => {
    const isCurrent = i === route.current_index;
    const icon = L.divIcon({
      className: "",
      html: `<div class="${isCurrent ? "map-pin-current" : "map-pin-plain"}"></div>`,
      iconSize: isCurrent ? [14, 14] : [10, 10],
    });
    const popupHtml = `
      <div class="map-popup">
        <div style="font-weight:600;">${point.event || "-"}${isCurrent ? " (current)" : ""}</div>
        <div>${point.label || "-"}</div>
        <div class="field-label" style="margin-top:4px;">${isCurrent ? "As of" : "Date"}</div>
        <div>${point.date || "-"} ${point.time && point.time !== "-" ? point.time : ""}</div>
      </div>
    `;
    L.marker([point.lat, point.lon], { icon }).addTo(map).bindPopup(popupHtml);
  });

  if (latLngs.length === 1) {
    map.setView(latLngs[0], 5);
  } else {
    map.fitBounds(latLngs, { padding: [30, 30] });
  }

  // The container was just attached to a page that may still be
  // reflowing; without this Leaflet sometimes renders tiles at the wrong
  // size until the next resize.
  setTimeout(() => map.invalidateSize(), 100);
}

// The API can send summary_fields either as an array of {label, value}
// pairs, or as a flat object (e.g. {container_number: "...", eta: "..."}).
// Normalize to an array of {label, value} so the renderer only has one
// shape to deal with.
function normalizeSummaryFields(summary_fields) {
  if (Array.isArray(summary_fields)) return summary_fields;
  if (summary_fields && typeof summary_fields === "object") {
    return Object.entries(summary_fields).map(([key, value]) => ({
      label: humanizeKey(key),
      value,
    }));
  }
  return [];
}

// The API can send each event either as an array already ordered to match
// event_columns, or as an object keyed by column name. Normalize to an
// array of values in event_columns order.
function normalizeEventRow(row, event_columns) {
  if (Array.isArray(row)) return row;
  if (row && typeof row === "object") {
    return (event_columns || []).map((col) => row[col]);
  }
  return [];
}

function humanizeKey(key) {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Builds a simple tab strip + panel switcher. `tabs` is [{label, build}],
// where `build` returns the panel's content element. Panels are built
// eagerly (so e.g. charts behind an inactive tab still get their canvas)
// but only the first is shown until clicked.
function buildTabs(tabs) {
  const wrap = el("div");
  const tabRow = el("div", { className: "tabs" });
  const panels = [];

  tabs.forEach((tab, i) => {
    const btn = el("button", { className: `tab-btn${i === 0 ? " active" : ""}`, text: tab.label });
    btn.type = "button";
    const panel = el("div", { className: `tab-panel${i === 0 ? " active" : ""}` }, [tab.build()]);
    btn.addEventListener("click", () => {
      tabRow.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      panel.classList.add("active");
      // Charts built while their tab was hidden have a zero-size canvas;
      // Chart.js needs an explicit resize once the panel is actually visible.
      requestAnimationFrame(() => activeCharts.forEach((chart) => chart.resize()));
    });
    tabRow.appendChild(btn);
    panels.push(panel);
  });

  wrap.appendChild(tabRow);
  panels.forEach((p) => wrap.appendChild(p));
  return wrap;
}

function buildMilestonesPanel(milestones) {
  if (!milestones || !milestones.length) {
    return el("p", { text: "No milestone events available." });
  }
  const list = el("div", { className: "milestone-list" });
  milestones.forEach((m) => {
    const item = el("div", { className: "milestone-item" });
    const left = el("div", {}, [
      el("div", { className: "milestone-event", text: m.event || "-" }),
      el("div", { className: "milestone-location", text: m.location || "-" }),
    ]);
    const when = m.time && m.time !== "-" ? `${m.date} · ${m.time}` : m.date || "-";
    const right = el("div", { className: "milestone-when", text: when });
    item.appendChild(left);
    item.appendChild(right);
    list.appendChild(item);
  });
  return list;
}

function buildFullTablePanel(events, event_columns) {
  const table = el("table", { className: "events" });
  const thead = el("thead");
  const headRow = el("tr");
  (event_columns || []).forEach((h) => headRow.appendChild(el("th", { text: h })));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  events.forEach((rawRow) => {
    const row = normalizeEventRow(rawRow, event_columns);
    const tr = el("tr");
    row.forEach((value) => tr.appendChild(el("td", { text: value || "-" })));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

// Turns leg_durations (from-event -> to-event, days) into cumulative
// [start, end] day offsets from the first milestone, for the Gantt chart.
function computeGanttSegments(legDurations) {
  let cursor = 0;
  return legDurations.map((leg) => {
    const start = cursor;
    const days = typeof leg.days === "number" ? leg.days : 0;
    const end = start + days;
    cursor = end;
    return { label: `${leg.from_event} → ${leg.to_event}`, start, end, days: leg.days };
  });
}

// Chart.js accepts an array of strings as a multi-line tick label, so wrap
// long leg names onto a few short lines instead of truncating them with "…".
function wrapLabel(label, maxLineLen = 24) {
  const words = label.split(" ");
  const lines = [];
  let current = "";
  words.forEach((word) => {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length > maxLineLen && current) {
      lines.push(current);
      current = word;
    } else {
      current = candidate;
    }
  });
  if (current) lines.push(current);
  return lines;
}

function buildChartsPanel(analytics) {
  const wrap = el("div", { className: "charts-grid" });
  const legDurations = (analytics.leg_durations || []).filter((l) => typeof l.days === "number");

  if (legDurations.length) {
    const segments = computeGanttSegments(legDurations);

    // Each row needs enough vertical room for a wrapped, multi-line label.
    const rowHeight = 70;
    const chartHeight = Math.max(220, segments.length * rowHeight);

    const ganttBlock = el("div", { className: "chart-block" });
    ganttBlock.appendChild(el("h3", { text: "Shipment progress timeline" }));
    const ganttWrap = el("div", { className: "chart-canvas-wrap" });
    ganttWrap.style.height = `${chartHeight}px`;
    const ganttCanvas = el("canvas");
    ganttWrap.appendChild(ganttCanvas);
    ganttBlock.appendChild(ganttWrap);
    wrap.appendChild(ganttBlock);

    const legBlock = el("div", { className: "chart-block" });
    legBlock.appendChild(el("h3", { text: "Transit days per leg" }));
    const legWrap = el("div", { className: "chart-canvas-wrap" });
    legWrap.style.height = `${chartHeight}px`;
    const legCanvas = el("canvas");
    legWrap.appendChild(legCanvas);
    legBlock.appendChild(legWrap);
    wrap.appendChild(legBlock);

    queueChartRender(() => {
      activeCharts.push(
        new Chart(ganttCanvas, {
          type: "bar",
          data: {
            labels: segments.map((s) => wrapLabel(s.label)),
            datasets: [
              {
                label: "Days",
                data: segments.map((s) => [s.start, s.end]),
                backgroundColor: CHART_PALETTE.brand,
                borderRadius: 4,
              },
            ],
          },
          options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: (ctx) => `${ctx.raw[1] - ctx.raw[0]} day(s)`,
                },
              },
            },
            scales: {
              x: {
                title: { display: true, text: "Days since first event", color: CHART_PALETTE.muted },
                ticks: { color: CHART_PALETTE.muted },
                grid: { color: CHART_PALETTE.border },
              },
              y: {
                ticks: { color: CHART_PALETTE.text, font: { size: 11 } },
                grid: { display: false },
              },
            },
          },
        })
      );

      activeCharts.push(
        new Chart(legCanvas, {
          type: "bar",
          data: {
            labels: segments.map((s) => wrapLabel(s.label)),
            datasets: [
              {
                label: "Days",
                data: segments.map((s) => s.days),
                backgroundColor: CHART_PALETTE.ok,
                borderRadius: 4,
              },
            ],
          },
          options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: {
                title: { display: true, text: "Days", color: CHART_PALETTE.muted },
                ticks: { color: CHART_PALETTE.muted },
                grid: { color: CHART_PALETTE.border },
              },
              y: {
                ticks: { color: CHART_PALETTE.text, font: { size: 11 } },
                grid: { display: false },
              },
            },
          },
        })
      );
    });
  }

  const modeBreakdown = analytics.mode_breakdown || {};
  const modeLabels = Object.keys(modeBreakdown);
  if (modeLabels.length) {
    const modeBlock = el("div", { className: "chart-block" });
    modeBlock.appendChild(el("h3", { text: "Movement type breakdown" }));
    const modeWrap = el("div", { className: "chart-canvas-wrap donut" });
    const modeCanvas = el("canvas");
    modeWrap.appendChild(modeCanvas);
    modeBlock.appendChild(modeWrap);
    wrap.appendChild(modeBlock);

    queueChartRender(() => {
      activeCharts.push(
        new Chart(modeCanvas, {
          type: "doughnut",
          data: {
            labels: modeLabels,
            datasets: [
              {
                data: modeLabels.map((k) => modeBreakdown[k]),
                backgroundColor: CHART_PALETTE.series,
                borderColor: CHART_PALETTE.card,
                borderWidth: 2,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { position: "bottom", labels: { color: CHART_PALETTE.text } },
            },
          },
        })
      );
    });
  }

  if (!wrap.childNodes.length) {
    wrap.appendChild(el("p", { text: "Not enough dated events to build charts yet." }));
  }

  return wrap;
}

// Chart.js needs its canvas already attached (and visible) to size itself
// correctly, so chart creation is deferred until after the result panel is
// unhidden (same reasoning as the Leaflet map init).
let pendingChartRenders = [];
function queueChartRender(fn) {
  pendingChartRenders.push(fn);
}

function renderResult(data) {
  const { events, event_columns, line_name, container_number, source_url } = data;
  const summary_fields = normalizeSummaryFields(data.summary_fields);

  const card = el("div", { className: "card" });
  card.appendChild(el("h2", { text: `${line_name}: ${container_number}` }));

  const grid = el("div", { className: "summary-grid" });
  summary_fields.forEach(({ label, value }) => {
    const wrap = el("div");
    wrap.appendChild(el("div", { className: "field-label", text: label }));
    wrap.appendChild(el("div", { className: "field-value", text: value || "-" }));
    grid.appendChild(wrap);
  });
  card.appendChild(grid);

  if (source_url) {
    card.appendChild(
      el("a", {
        className: "source-link",
        href: source_url,
        target: "_blank",
        rel: "noopener noreferrer",
        text: `View on ${line_name} →`,
      })
    );
  }

  resultEl.appendChild(card);

  const route = data.route;
  let mapContainer = null;
  let mapCard = null;
  if (route && route.points && route.points.length) {
    mapCard = el("div", { className: "card" });
    mapCard.appendChild(el("h2", { text: "Route Map" }));
    mapContainer = el("div", { className: "map-container" });
    mapCard.appendChild(mapContainer);
  }

  let timelineCard = null;
  if (events && events.length) {
    timelineCard = el("div", { className: "card" });
    timelineCard.appendChild(el("h2", { text: "Event timeline" }));

    const analytics = data.analytics || {};
    const tabs = [
      { label: "Milestones", build: () => buildMilestonesPanel(analytics.milestones) },
      { label: "Events", build: () => buildFullTablePanel(events, event_columns) },
      { label: "Charts", build: () => buildChartsPanel(analytics) },
    ];
    timelineCard.appendChild(buildTabs(tabs));
  }

  if (mapCard && timelineCard) {
    const row = el("div", { className: "route-timeline-row" }, [mapCard, timelineCard]);
    resultEl.appendChild(row);
  } else if (mapCard) {
    resultEl.appendChild(mapCard);
  } else if (timelineCard) {
    resultEl.appendChild(timelineCard);
  }

  resultEl.hidden = false;

  if (mapContainer) {
    initRouteMap(mapContainer, route);
  }

  pendingChartRenders.forEach((fn) => fn());
  pendingChartRenders = [];
}

form.addEventListener("submit", async (evt) => {
  evt.preventDefault();
  clearResult();

  const line = document.getElementById("line").value;
  const container = document.getElementById("container").value.trim();

  if (!container) {
    setStatus("Please enter a container number.", "error");
    return;
  }

  submitBtn.disabled = true;

  // Maersk drives a real browser through the carrier's own site and can
  // realistically take a couple of minutes, especially on a cold search -
  // a static "loading" message for that long reads as stuck, so keep
  // updating it with elapsed time to make the wait legible.
  let progressTimer = null;
  if (line === "maersk") {
    const startedAt = Date.now();
    const tick = () => {
      const secs = Math.round((Date.now() - startedAt) / 1000);
      setStatus(
        `Fetching from Maersk — this can take a couple of minutes… (${secs}s elapsed)`,
        "loading"
      );
    };
    tick();
    progressTimer = setInterval(tick, 5000);
  } else {
    setStatus("Fetching tracking details…", "loading");
  }

  let data;
  try {
    const resp = await fetch("/api/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line, container }),
    });
    data = await resp.json();

    if (!resp.ok) {
      setStatus(data.error || "Something went wrong.", "error");
      return;
    }
  } catch (err) {
    setStatus("Network error — couldn't reach the server.", "error");
    return;
  } finally {
    submitBtn.disabled = false;
    if (progressTimer) clearInterval(progressTimer);
  }

  // Rendering is deliberately outside the fetch try/catch: a response that
  // doesn't match the expected shape should say so, not claim the network failed.
  try {
    setStatus(null);
    renderResult(data);
  } catch (err) {
    console.error("Could not render tracking result:", err, data);
    setStatus(`Got a response but couldn't display it: ${err.message}`, "error");
  }
});