(() => {
"use strict";

const $ = (selector) => document.querySelector(selector);
const charts = new Map();
let fileId = new URLSearchParams(window.location.search).get("fileId");
let activeSimulationId = null;
let pollTimer = null;
let lastRowCount = -1;

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "-";
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** index);
  return `${amount.toFixed(index === 0 || amount >= 100 ? 0 : 1)} ${units[index]}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function updateFileContext(files, config) {
  const indexEl = $("#indexNodeName");
  const storeEl = $("#storeNodeName");
  if (indexEl) indexEl.textContent = config.index_label;
  if (storeEl) storeEl.textContent = config.store_label;
  if (!fileId) {
    $("#fileContext").hidden = true;
    return;
  }
  const file = files.find((item) => item.file_id === fileId);
  const context = $("#fileContext");
  if (!file) {
    $("#fileNotice").textContent = "The upload context could not be found in this storage vault. You can still run a simulated workload.";
    $("#fileNotice").hidden = false;
    return;
  }
  $("#fileName").textContent = file.name;
  $("#fileDetails").textContent = `${formatBytes(file.size)} · ${Number(file.chunks).toLocaleString()} chunks · ${file.chunker.toUpperCase()} · ${new Date(file.created_at).toLocaleString()}`;
  $("#backendName").textContent = config.backend === "aws" ? "AWS" : "local";
  context.hidden = false;
}

function setRunStatus(message, state = "") {
  const status = $("#runStatus");
  status.textContent = message;
  status.className = `run-status${state ? ` is-${state}` : ""}`;
}

function setStage(stage) {
  $("#metricStage").textContent = stage || "-";
  for (const item of document.querySelectorAll(".stage-list [data-stage]")) {
    item.classList.toggle("is-active", item.dataset.stage === stage);
    item.classList.toggle("is-done", stage === "10x" && item.dataset.stage === "1x"
      || stage === "100x" && item.dataset.stage !== "100x");
  }
}

function updateLiveMetrics(rows) {
  if (!rows.length) return;
  const row = rows[rows.length - 1];
  setStage(row.stage);
  $("#metricFiles").textContent = Number(row.files_per_second).toLocaleString();
  $("#metricChunks").textContent = Number(row.chunks_per_second).toLocaleString();
  $("#metricDedup").textContent = `${Number(row.dedup_ratio).toFixed(2)}×`;
  $("#metricLatency").textContent = `${Number(row.index_lookup_latency_p95_ms).toFixed(2)} ms`;
}

function makeDatasets(rows, field, label, color, mode) {
  return {
    label: mode === "virtual" ? `${label} · SIMULATED` : label,
    data: rows.map((row) => row.mode === mode ? row[field] : null),
    borderColor: color,
    backgroundColor: color,
    borderWidth: 2.5,
    pointRadius: 3,
    pointHoverRadius: 5,
    tension: 0.3,
    borderDash: mode === "virtual" ? [5, 4] : [],
    spanGaps: true,
  };
}

function renderChart(id, type, datasets, labels) {
  if (typeof Chart === "undefined") {
    throw new Error("Chart.js did not load. Check your internet connection and reload this page.");
  }
  const existing = charts.get(id);
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets = datasets;
    existing.update("none");
    return;
  }
  charts.set(id, new Chart(document.getElementById(id), {
    type,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: {
          labels: {
            color: "#1e293b",
            boxWidth: 14,
            usePointStyle: true,
            font: { size: 12, weight: "bold", family: "system-ui, sans-serif" },
            padding: 16,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: "#64748b",
            maxRotation: 0,
            autoSkip: true,
            font: { size: 11, family: "system-ui, sans-serif" },
          },
          grid: { color: "#f1f5f9" },
        },
        y: {
          beginAtZero: true,
          ticks: {
            color: "#64748b",
            font: { size: 11, family: "system-ui, sans-serif" },
          },
          grid: { color: "#f1f5f9" },
        },
      },
    },
  }));
}

function renderMetrics(rows) {
  if (rows.some((row) => row.mode === "virtual" && (row.label !== "simulated" || row.simulated !== true))) {
    throw new Error("Virtual metrics are missing their simulated label.");
  }
  const labels = rows.map((row) => `${row.elapsed_second}s · ${row.stage}`);
  const modes = [...new Set(rows.map((row) => row.mode))];
  renderChart("throughput", "line", [
    ...modes.map((mode) => makeDatasets(rows, "chunks_per_second", "Chunks / sec", "#4936a5", mode)),
    ...modes.map((mode) => makeDatasets(rows, "files_per_second", "Files / sec", "#d28a16", mode)),
  ], labels);
  renderChart("dedup", "line", modes.map((mode) => makeDatasets(rows, "dedup_ratio", "Logical / physical", "#4936a5", mode)), labels);
  renderChart("latency", "line", [
    ...modes.map((mode) => makeDatasets(rows, "index_lookup_latency_p50_ms", "p50 ms", "#a99cdb", mode)),
    ...modes.map((mode) => makeDatasets(rows, "index_lookup_latency_p95_ms", "p95 ms", "#4936a5", mode)),
    ...modes.map((mode) => makeDatasets(rows, "index_lookup_latency_p99_ms", "p99 ms", "#d28a16", mode)),
    ...modes.filter((mode) => rows.some((row) => row.mode === mode && row.simulated_store_latency_ms !== undefined))
      .map((mode) => makeDatasets(rows, "simulated_store_latency_ms", "Store latency ms", "#7564bd", mode)),
  ], labels);
  renderChart("chunks", "bar", [
    ...modes.map((mode) => makeDatasets(rows, "unique_chunks", "Unique chunks", "#4936a5", mode)),
    ...modes.map((mode) => makeDatasets(rows, "duplicate_chunks", "Duplicate chunks", "#d28a16", mode)),
  ], labels);
  updateLiveMetrics(rows);
  $("#chartStatus").textContent = `${rows.length} per-second observations loaded. All virtual rows are labelled SIMULATED.`;
}

function setTopologyRunning(running) {
  $("#topology").classList.toggle("is-running", running);
}

async function pollSimulation(simulationId) {
  try {
    const job = await api(`/api/simulations/${simulationId}`);
    if (job.rows.length !== lastRowCount) {
      renderMetrics(job.rows);
      lastRowCount = job.rows.length;
    }
    if (job.rows.length) {
      const currentStage = job.rows[job.rows.length - 1].stage;
      setRunStatus(`Running · ${currentStage} stage · ${job.rows.length} seconds recorded`, "running");
    }
    if (job.status === "completed") {
      window.clearTimeout(pollTimer);
      activeSimulationId = null;
      $("#runButton").disabled = false;
      setTopologyRunning(false);
      setRunStatus("Complete · virtual results are simulated and separate from storage totals.", "complete");
      $("#chartStatus").textContent = `Simulation complete · ${job.rows.length} per-second observations · SIMULATED`;
      return;
    }
    if (job.status === "failed") {
      window.clearTimeout(pollTimer);
      activeSimulationId = null;
      $("#runButton").disabled = false;
      setTopologyRunning(false);
      setRunStatus("Simulation failed.", "error");
      $("#simulationError").textContent = job.error || "The simulation failed.";
      $("#simulationError").hidden = false;
      return;
    }
    pollTimer = window.setTimeout(() => pollSimulation(simulationId), 650);
  } catch (error) {
    window.clearTimeout(pollTimer);
    activeSimulationId = null;
    $("#runButton").disabled = false;
    setTopologyRunning(false);
    setRunStatus("Could not read simulation status.", "error");
    $("#simulationError").textContent = error.message;
    $("#simulationError").hidden = false;
  }
}

async function startSimulation() {
  if (activeSimulationId) return;
  $("#simulationError").hidden = true;
  $("#runButton").disabled = true;
  $("#topology").classList.add("is-running");
  $("#metricStage").textContent = "Starting";
  lastRowCount = -1;
  setRunStatus("Starting seeded virtual workload…", "running");
  try {
    const job = await api("/api/simulations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stage_seconds: Number($("#stageSeconds").value),
        duplicate_ratio: Number($("#duplicateRatio").value),
        seed: 1,
        source_file_id: fileId,
      }),
    });
    activeSimulationId = job.simulation_id;
    await pollSimulation(activeSimulationId);
  } catch (error) {
    activeSimulationId = null;
    $("#runButton").disabled = false;
    setTopologyRunning(false);
    setRunStatus("Could not start simulation.", "error");
    $("#simulationError").textContent = error.message;
    $("#simulationError").hidden = false;
  }
}

async function importMetricsFile(file) {
  if (!file) return;
  try {
    const rows = (await file.text()).split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
    renderMetrics(rows);
  } catch (error) {
    $("#chartStatus").textContent = `Could not read metrics: ${error.message}`;
  }
}

async function initialize() {
  try {
    const [config, files] = await Promise.all([api("/api/config"), api("/api/files")]);
    updateFileContext(files.files, config);
  } catch (error) {
    $("#fileNotice").textContent = `File context could not be loaded: ${error.message}`;
    $("#fileNotice").hidden = false;
  }
}

window.addEventListener("chunkwise:file-uploaded", async (event) => {
  fileId = event.detail.fileId;
  try {
    const [config, files] = await Promise.all([api("/api/config"), api("/api/files")]);
    updateFileContext(files.files, config);
  } catch (error) {
    $("#fileNotice").textContent = `The file was uploaded, but its simulation context could not be loaded: ${error.message}`;
    $("#fileNotice").hidden = false;
  }
});

$("#runButton").addEventListener("click", startSimulation);
$("#metricsFile").addEventListener("change", (event) => importMetricsFile(event.target.files[0]));
initialize();
})();
