"use strict";

// ----------------------------------------------------------------
// ChunkWise frontend — Poorvaja (Member A)
// Design system: Warm Ivory palette
// ----------------------------------------------------------------

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

const fileInput  = $("#fileInput");
const dropZone   = $("#dropZone");
const uploadButton = $("#uploadButton");
let selectedFile = null;
let pendingUploadKey = null;
let pendingUploadFingerprint = null;
let refreshGeneration = 0;

// ----------------------------------------------------------------
// Sidebar navigation
// ----------------------------------------------------------------
const SECTION_TITLES = {
  overview:     "Overview",
  upload:       "Upload Files",
  files:        "Files",
  chunks:       "Chunk Explorer",
  simulation:   "Simulation",
  optimization: "Optimization",
  activity:     "Activity",
  analytics:    "Storage Analytics",
  settings:     "Settings",
  cookbook:     "Cookbook & Recipes",
};

function activateSection(name) {
  // Hide all sections
  for (const key of Object.keys(SECTION_TITLES)) {
    const el = $(`#section-${key}`);
    if (el) el.hidden = true;
  }
  // Show requested
  const target = $(`#section-${name}`);
  if (target) target.hidden = false;

  // Update sidebar active state
  $$(".sidebar-nav a").forEach(a => {
    a.classList.toggle("is-active", a.dataset.section === name);
  });

  // Update topbar title
  const titleEl = $("#topbarTitle");
  if (titleEl) titleEl.textContent = SECTION_TITLES[name] || name;

  if (name === "analytics") {
    renderAnalytics();
  }
  if (name === "chunks" && cachedFilesList.length) {
    populateChunkFileSelect(cachedFilesList);
  }
}

// Sidebar link clicks
$$(".sidebar-nav a").forEach(link => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    const section = link.dataset.section;
    if (section) activateSection(section);
  });
});

// Hash-based navigation (from links like href="#simulation")
function navigateFromHash() {
  const hash = window.location.hash.replace("#", "");
  if (hash && SECTION_TITLES[hash]) {
    activateSection(hash);
  }
}
window.addEventListener("hashchange", navigateFromHash);

// ----------------------------------------------------------------
// Utilities
// ----------------------------------------------------------------
function formatBytes(value) {
  if (value === 0) return "0 B";
  if (!Number.isFinite(value) || value < 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** index);
  return `${amount.toFixed(index === 0 || amount >= 100 ? 0 : 1)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json")
    ? await response.json()
    : await response.blob();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showGlobalError(message) {
  const node = $("#globalError");
  node.textContent = message;
  node.hidden = false;
}

function setUploadMessage(message, kind) {
  const node = $("#uploadMessage");
  node.textContent = message;
  node.className = `inline-message ${kind}`;
  node.hidden = false;
}

// ----------------------------------------------------------------
// Stats / UI refresh
// ----------------------------------------------------------------
async function refreshStats() {
  const stats = await api("/api/stats");
  const ratio = Number(stats.dedup_ratio);
  const savedPct = Number(stats.storage_saved_pct);

  $("#statLogical").textContent  = formatBytes(stats.logical_bytes);
  $("#statPhysical").textContent = formatBytes(stats.physical_bytes);
  $("#statRatio").textContent    = `${ratio.toFixed(2)}×`;
  $("#statSaved").textContent    = `${savedPct.toFixed(1)}%`;

  // Human-friendly dedup explanation
  const ratioDesc = ratio >= 1.1
    ? `${ratio.toFixed(1)}× means every ${ratio.toFixed(1)} GB stored as 1 GB — ${savedPct.toFixed(0)}% space saved`
    : "Logical bytes per stored byte";
  const ratioEl = $("#statRatioDesc");
  if (ratioEl) ratioEl.textContent = ratioDesc;

  const savedEl = $("#statSavedDetail");
  if (savedEl) {
    const savedBytes = Math.max(0, stats.logical_bytes - stats.physical_bytes);
    savedEl.textContent = `${formatBytes(savedBytes)} less stored than original`;
  }

  $("#statFiles").textContent  = Number(stats.files).toLocaleString();
  $("#statChunks").textContent = Number(stats.unique_chunks).toLocaleString();

  // Composition panel
  const compositionLogical = $("#compositionLogical");
  if (compositionLogical) {
    compositionLogical.textContent = formatBytes(stats.logical_bytes);
    $("#compositionPhysical").textContent = formatBytes(stats.physical_bytes);
    $("#compositionSaved").textContent    = formatBytes(Math.max(0, stats.logical_bytes - stats.physical_bytes));
    const storedShare = stats.logical_bytes > 0
      ? Math.min(100, (stats.physical_bytes / stats.logical_bytes) * 100) : 0;
    const bar = $("#compositionBar");
    if (bar) bar.style.width = `${storedShare}%`;
    const donut = $("#compositionDonutRing");
    const pctText = $("#compositionPercentText");
    if (donut && pctText) {
      const circum = 251.2;
      const strokeVal = (storedShare / 100) * circum;
      donut.setAttribute("stroke-dasharray", `${strokeVal} ${circum}`);
      pctText.textContent = `${Math.round(storedShare)}%`;
    }
    const noteEl = $("#compositionNote");
    if (noteEl) {
      noteEl.textContent = stats.files
        ? `${Number(stats.unique_chunks).toLocaleString()} unique chunks across ${Number(stats.files).toLocaleString()} committed files.`
        : "Storage totals will appear when data is available.";
    }
  }

  // Real storage usage in sidebar system card
  const sidebarDonutFill = $("#sidebarDonutFill");
  const sidebarDonutPct  = $("#sidebarDonutPct");
  const sidebarDonutVal  = $("#sidebarDonutVal");
  if (sidebarDonutVal) {
    const quotaBytes = 5 * 1024 * 1024 * 1024; // 5 GB baseline
    const physBytes = Number(stats.physical_bytes) || 0;
    const usagePct = quotaBytes > 0 ? Math.min(100, (physBytes / quotaBytes) * 100) : 0;
    if (sidebarDonutPct) sidebarDonutPct.textContent = `${Math.round(usagePct)}%`;
    if (sidebarDonutFill) sidebarDonutFill.setAttribute("stroke-dasharray", `${usagePct.toFixed(1)}, 100`);
    sidebarDonutVal.textContent = `${formatBytes(physBytes)} / 5 GB`;
  }

  $("#statsGrid")?.setAttribute("aria-busy", "false");
}

function renderFiles(files, bodyId = "filesBody", emptyId = "filesEmpty", tableId = "filesTableWrap", loadId = "filesLoading", countId = "fileCount") {
  const body = $(`#${bodyId}`);
  if (!body) return;
  body.replaceChildren();
  const countEl = $(`#${countId}`);
  if (countEl) countEl.textContent = files.length.toLocaleString();
  const empty = $(`#${emptyId}`);
  const table = $(`#${tableId}`);
  if (!files.length) {
    if (empty) empty.hidden = false;
    if (table) table.hidden = true;
    return;
  }
  if (empty) empty.hidden = true;
  if (table) table.hidden = false;

  for (const file of files) {
    const row = document.createElement("tr");
    const safeName = escapeHtml(file.name);
    const ext = (file.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
    row.innerHTML = `
      <td><div class="file-name">
        <span class="file-glyph">${escapeHtml(ext)}</span>
        <span title="${safeName}">${safeName}</span>
      </div></td>
      <td>${formatBytes(file.size)}</td>
      <td><span class="method-tag">${escapeHtml(file.chunker)}</span></td>
      <td>${escapeHtml(dateLabel(file.created_at))}</td>
      <td><span class="file-id">${escapeHtml(file.file_id)}</span></td>
      <td><div class="row-actions">
        <button class="action-button" data-action="inspect"  data-id="${escapeHtml(file.file_id)}">Inspect</button>
        <button class="action-button" data-action="verify"   data-id="${escapeHtml(file.file_id)}">Verify</button>
        <button class="action-button" data-action="download" data-id="${escapeHtml(file.file_id)}">Restore</button>
      </div></td>`;
    body.append(row);
  }

  // Populate chunk selector dropdown
  populateChunkFileSelect(files);

  // Populate files section too
  if (bodyId === "filesBody") {
    renderFiles(files, "filesBody2", "filesEmpty2", "filesTableWrap2", "filesLoading2", "fileCount2");
    renderAnalyticsFiles(files);
    renderFullActivity(files);
  }

  // Update sidebar activity
  updateActivity(files);
}

function renderAnalyticsFiles(files) {
  const body = $("#analyticsFilesBody");
  const empty = $("#analyticsFilesEmpty");
  if (!body) return;
  body.replaceChildren();
  if (!files || !files.length) {
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  files.forEach(f => {
    const row = document.createElement("tr");
    const safeName = escapeHtml(f.name);
    const ext = (f.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
    const totalChunks = Number(f.chunks) || 0;
    const uniqueChunks = Number(f.unique_chunks) || totalChunks;
    const avgChunkSize = totalChunks > 0 ? formatBytes(Math.round(f.size / totalChunks)) : "-";
    const dedupRatio = (uniqueChunks > 0 && totalChunks > uniqueChunks)
      ? `${(totalChunks / uniqueChunks).toFixed(2)}×`
      : "1.00×";
    const yieldClass = (totalChunks > uniqueChunks) ? "yield-high" : "yield-base";

    row.innerHTML = `
      <td>
        <div class="file-name">
          <span class="file-glyph">${escapeHtml(ext)}</span>
          <span title="${safeName}">${safeName}</span>
        </div>
      </td>
      <td><strong>${formatBytes(f.size)}</strong></td>
      <td><span class="badge-chunks">${totalChunks} chunks</span></td>
      <td><span class="badge-unique">${uniqueChunks} unique</span></td>
      <td>${avgChunkSize}</td>
      <td><span class="method-tag">${escapeHtml(f.chunker || "cdc")}</span></td>
      <td><span class="dedup-pill ${yieldClass}">${dedupRatio}</span></td>
      <td>${escapeHtml(dateLabel(f.created_at))}</td>
      <td>
        <button class="action-button" data-action="inspect" data-id="${escapeHtml(f.file_id)}">Inspect Chunks →</button>
      </td>
    `;
    body.append(row);
  });
}

const inMemoryActionLogs = [];

function logAuditEvent(type, resource, detail, status = "SUCCESS") {
  inMemoryActionLogs.unshift({
    type,
    resource,
    detail,
    status,
    timestamp: new Date().toISOString(),
  });
  if (cachedFilesList) renderFullActivity(cachedFilesList);
}

function renderFullActivity(files) {
  const tbody = $("#activityLogTableBody");
  const empty = $("#activityLogEmpty");
  const countEl = $("#actFilterCount");
  if (!tbody) return;
  tbody.replaceChildren();

  const allEvents = [];
  inMemoryActionLogs.forEach(e => allEvents.push(e));

  if (files && files.length) {
    files.forEach(f => {
      allEvents.push({
        type: "UPLOAD",
        resource: f.name,
        detail: `${Number(f.chunks || 0)} chunks · ${f.chunker.toUpperCase()} chunker`,
        size: formatBytes(f.size),
        status: "COMMITTED",
        timestamp: f.created_at,
      });
    });
  }

  const totalEvents = allEvents.length;
  const uploadEvents = allEvents.filter(e => e.type === "UPLOAD").length;
  const dedupChunks = files ? files.reduce((acc, f) => acc + (f.chunks || 0), 0) : 0;

  if ($("#actStatTotal")) $("#actStatTotal").textContent = totalEvents.toLocaleString();
  if ($("#actStatUploads")) $("#actStatUploads").textContent = uploadEvents.toLocaleString();
  if ($("#actStatDedup")) $("#actStatDedup").textContent = dedupChunks.toLocaleString();
  if (countEl) countEl.textContent = `${totalEvents} events`;

  if (!allEvents.length) {
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  const activeFilterBtn = document.querySelector(".act-filter-pill.is-active");
  const activeFilter = activeFilterBtn ? activeFilterBtn.dataset.filter : "all";

  const filtered = allEvents.filter(e => {
    if (activeFilter === "all") return true;
    if (activeFilter === "upload") return e.type === "UPLOAD";
    if (activeFilter === "restore") return e.type === "RESTORE" || e.type === "DOWNLOAD";
    if (activeFilter === "verify") return e.type === "VERIFY";
    return true;
  });

  filtered.forEach(ev => {
    const tr = document.createElement("tr");
    const tagClass = ev.type === "UPLOAD" ? "tag-upload" : (ev.type === "VERIFY" ? "tag-verify" : "tag-restore");
    tr.innerHTML = `
      <td><span class="event-tag ${tagClass}">${escapeHtml(ev.type)}</span></td>
      <td><strong>${escapeHtml(ev.resource)}</strong></td>
      <td>${escapeHtml(ev.detail || "-")}</td>
      <td>${escapeHtml(ev.size || "-")}</td>
      <td>${escapeHtml(dateLabel(ev.timestamp))}</td>
      <td><span class="status-success-pill">● ${escapeHtml(ev.status || "OK")}</span></td>
    `;
    tbody.append(tr);
  });
}

function updateActivity(files) {
  const list = $("#activityList");
  if (!list || !files.length) return;
  list.replaceChildren();
  files.slice(0, 5).forEach(f => {
    const item = document.createElement("div");
    item.className = "activity-item";
    const timeAgo = dateLabel(f.created_at);
    item.innerHTML = `
      <div class="activity-dot upload"></div>
      <div class="activity-body">
        <div class="activity-title">${escapeHtml(f.name)} uploaded</div>
        <div class="activity-detail">${formatBytes(f.size)} · ${Number(f.chunks || 0).toLocaleString()} chunks</div>
      </div>
      <span class="activity-time">${escapeHtml(timeAgo)}</span>`;
    list.append(item);
  });
}

async function refreshFiles() {
  const generation = ++refreshGeneration;
  for (const id of ["filesLoading", "filesLoading2"]) {
    const el = $(`#${id}`);
    if (el) el.hidden = false;
  }
  for (const id of ["filesTableWrap", "filesEmpty", "filesError", "filesTableWrap2", "filesEmpty2", "filesError2"]) {
    const el = $(`#${id}`);
    if (el) el.hidden = true;
  }
  try {
    const result = await api("/api/files");
    if (generation === refreshGeneration) {
      renderFiles(result.files);
    }
  } catch (error) {
    if (generation === refreshGeneration) {
      for (const id of ["filesError", "filesError2"]) {
        const el = $(`#${id}`);
        if (el) { el.textContent = error.message; el.hidden = false; }
      }
    }
  } finally {
    if (generation === refreshGeneration) {
      for (const id of ["filesLoading", "filesLoading2"]) {
        const el = $(`#${id}`);
        if (el) el.hidden = true;
      }
    }
  }
}

async function refreshAll() {
  $("#globalError").hidden = true;
  try {
    const [, , , config] = await Promise.all([
      refreshStats(), refreshFiles(), api("/api/health"), api("/api/config"),
    ]);
    const uploadLimit = $("#uploadLimit");
    if (uploadLimit) uploadLimit.textContent = `Maximum upload size: ${formatBytes(config.max_upload_bytes)}`;

    const isAWS = config.backend === "aws";
    const backendText = isAWS ? "AWS vault" : "Local vault";

    // Topbar
    const connLabel = $("#connectionLabel");
    if (connLabel) connLabel.textContent = "Storage online";
    const connStatus = $("#connectionStatus");
    if (connStatus) connStatus.classList.remove("is-offline");

    // Sidebar
    const sidebarHealth = $("#sidebarHealthLabel");
    if (sidebarHealth) sidebarHealth.textContent = "All Systems Operational";
    const sidebarDot = $("#sidebarHealthDot");
    if (sidebarDot) sidebarDot.classList.remove("is-offline");
    const sidebarBackend = $("#sidebarBackendLabel");
    if (sidebarBackend) sidebarBackend.textContent = backendText;

    // Settings
    const settingsBackend = $("#settingsBackend");
    if (settingsBackend) settingsBackend.textContent = backendText;
    const settingsConn = $("#settingsConnection");
    if (settingsConn) settingsConn.textContent = "Online";

  } catch (error) {
    $("#statsGrid")?.setAttribute("aria-busy", "false");
    const connLabel = $("#connectionLabel");
    if (connLabel) connLabel.textContent = "Storage offline";
    const connStatus = $("#connectionStatus");
    if (connStatus) connStatus.classList.add("is-offline");
    const sidebarDot = $("#sidebarHealthDot");
    if (sidebarDot) sidebarDot.classList.add("is-offline");
    const sidebarHealth = $("#sidebarHealthLabel");
    if (sidebarHealth) sidebarHealth.textContent = "Storage offline";
    showGlobalError(`Storage could not be loaded: ${error.message}`);
  }
}

// ----------------------------------------------------------------
// File selection / upload
// ----------------------------------------------------------------
function uploadKeyStorageKey(fingerprint) {
  return `chunkwise-pending-upload:${fingerprint}`;
}

function selectFile(file) {
  if (!file) return;
  selectedFile = file;
  const fingerprint = `${file.name}:${file.size}:${file.lastModified}`;
  if (pendingUploadFingerprint !== fingerprint) {
    pendingUploadFingerprint = fingerprint;
    try { pendingUploadKey = sessionStorage.getItem(uploadKeyStorageKey(fingerprint)); }
    catch { pendingUploadKey = null; }
  }
  const selEl = $("#selectedFile");
  if (selEl) { selEl.textContent = `${file.name} · ${formatBytes(file.size)}`; selEl.hidden = false; }
  uploadButton.disabled = false;
  const msgEl = $("#uploadMessage");
  if (msgEl) msgEl.hidden = true;
  const nextEl = $("#uploadNextSteps");
  if (nextEl) nextEl.hidden = true;
}

function uploadSelectedFile() {
  if (!selectedFile) return;
  const file = selectedFile;
  pendingUploadKey ||= crypto.randomUUID();
  try { sessionStorage.setItem(uploadKeyStorageKey(pendingUploadFingerprint), pendingUploadKey); }
  catch { /* ignore */ }

  const chunker = document.querySelector('input[name="chunker"]:checked').value;
  const progress = $("#uploadProgress");
  if (progress) progress.hidden = false;
  uploadButton.disabled = true;
  const msgEl = $("#uploadMessage");
  if (msgEl) msgEl.hidden = true;
  const labelEl = $("#progressLabel");
  if (labelEl) labelEl.textContent = "Uploading and indexing";
  const pctEl = $("#progressPercent");
  if (pctEl) pctEl.textContent = "0%";
  const barEl = $("#progressBar");
  if (barEl) barEl.style.width = "0%";

  const request = new XMLHttpRequest();
  request.open("POST", "/api/files");
  request.setRequestHeader("Content-Type", "application/octet-stream");
  request.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
  request.setRequestHeader("X-Chunker", chunker);
  request.setRequestHeader("Idempotency-Key", pendingUploadKey);
  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
    if (pctEl) pctEl.textContent = `${percent}%`;
    if (barEl) barEl.style.width = `${percent}%`;
  });
  request.addEventListener("load", async () => {
    uploadButton.disabled = false;
    if (progress) progress.hidden = true;
    let response;
    try { response = JSON.parse(request.responseText); } catch { response = {}; }
    if (request.status < 200 || request.status >= 300) {
      setUploadMessage(response.error || `Upload failed (${request.status})`, "error");
      return;
    }
    selectedFile = null;
    try { sessionStorage.removeItem(uploadKeyStorageKey(pendingUploadFingerprint)); } catch { /* ignore */ }
    pendingUploadKey = null;
    pendingUploadFingerprint = null;
    fileInput.value = "";
    const selEl = $("#selectedFile");
    if (selEl) selEl.hidden = true;
    uploadButton.disabled = true;
    setUploadMessage(
      `${response.name} stored · ${Number(response.chunks).toLocaleString()} chunks · ${formatBytes(response.new_bytes)} new bytes`,
      "success"
    );
    const nextEl = $("#uploadNextSteps");
    if (nextEl) {
      const simLink = $("#simulationLink");
      if (simLink) simLink.href = "#simulation";
      nextEl.hidden = false;
    }
    window.dispatchEvent(new CustomEvent("chunkwise:file-uploaded", { detail: { fileId: response.file_id } }));
    await refreshAll();
  });
  request.addEventListener("error", () => {
    uploadButton.disabled = false;
    if (progress) progress.hidden = true;
    setUploadMessage("Network error. Retry; the same request key prevents duplicate records.", "error");
  });
  request.send(file);
}

async function downloadFile(fileId, button) {
  button.disabled = true;
  try {
    const blob = await api(`/api/files/${encodeURIComponent(fileId)}/download`);
    const row = button.closest("tr");
    const filename = row?.querySelector(".file-name span:last-child")?.textContent || fileId;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  } catch (error) { showGlobalError(`Restore failed: ${error.message}`); }
  finally { button.disabled = false; }
}

async function verifyFile(fileId, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Checking…";
  try {
    const result = await api(`/api/files/${encodeURIComponent(fileId)}/verify`, { method: "POST" });
    button.textContent = result.verified ? "Verified ✓" : "Failed";
    button.title = `${formatBytes(result.size)} verified`;
  } catch (error) {
    button.textContent = "Error";
    button.title = error.message;
  } finally {
    window.setTimeout(() => { button.textContent = original; button.disabled = false; }, 2200);
  }
}

// ----------------------------------------------------------------
// Pipeline simulation controls (Breakscale 5-Node Topology)
// ----------------------------------------------------------------
const STAGE_COUNT = 5;
let simCurrentStage = 0;    // 0 = not started, 1-10 = stage index, 11 = done
let simPlaying = false;
let simTimer = null;

function getStageSeconds() {
  const el = $("#stageSeconds");
  return el ? parseInt(el.value, 10) * 1000 : 1500;
}

function getStageNode(i)   { return document.querySelector(`.pipeline-node[data-stage="${i}"]`); }
function getArrow(i)        { return $(`#arrow-${i}`); }
function getStagePill(i)    { return $(`#stage-status-${i}`); }

function resetSimulation() {
  clearInterval(simTimer);
  simPlaying = false;
  simCurrentStage = 0;
  for (let i = 1; i <= STAGE_COUNT; i++) {
    const node = getStageNode(i);
    if (node) {
      node.className = "pipeline-node is-pending";
    }
    const pill = getStagePill(i);
    if (pill) { pill.textContent = "Pending"; pill.className = "node-status-pill pending"; }
    const arrow = getArrow(i);
    if (arrow) arrow.className = "pipeline-arrow-line";
  }
  updateSimButtons();
  const badge1 = $("#branchDupBadge");
  const badge2 = $("#branchCompBadge");
  if (badge1) badge1.style.display = "none";
  if (badge2) badge2.style.display = "none";
}

function activateStage(i) {
  if (i < 1 || i > STAGE_COUNT) return;
  const node = getStageNode(i);
  const pill = getStagePill(i);
  const arrow = getArrow(i - 1);  // arrow pointing INTO this node

  // Mark previous stages done
  for (let j = 1; j < i; j++) {
    const prevNode = getStageNode(j);
    if (prevNode) prevNode.className = "pipeline-node is-done";
    const prevPill = getStagePill(j);
    if (prevPill) { prevPill.textContent = "Done"; prevPill.className = "node-status-pill done"; }
    const prevArrow = getArrow(j);
    if (prevArrow) prevArrow.className = "pipeline-arrow-line is-done";
  }

  // Active node
  if (node) node.className = "pipeline-node is-active";
  if (pill) { pill.textContent = "Running…"; pill.className = "node-status-pill active"; }
  if (arrow) arrow.className = "pipeline-arrow-line is-active";

  // Show callout badges at appropriate stages
  const badge1 = $("#branchDupBadge");
  const badge2 = $("#branchCompBadge");
  if (i === 6 && badge1) {
    badge1.style.display = "flex";
    badge1.style.display = "block";
  }
  if (i === 7 && badge2) {
    badge2.style.display = "block";
  }

  updateSimButtons();
}

function completeAllStages() {
  for (let i = 1; i <= STAGE_COUNT; i++) {
    const node = getStageNode(i);
    if (node) node.className = "pipeline-node is-done";
    const pill = getStagePill(i);
    if (pill) { pill.textContent = "Done"; pill.className = "node-status-pill done"; }
    const arrow = getArrow(i);
    if (arrow) arrow.className = "pipeline-arrow-line is-done";
  }
  simPlaying = false;
  updateSimButtons();
}

function updateSimButtons() {
  const playBtn  = $("#simPlayBtn");
  const pauseBtn = $("#simPauseBtn");
  const nextBtn  = $("#simNextBtn");
  const prevBtn  = $("#simPrevBtn");
  if (!playBtn) return;
  const done = simCurrentStage > STAGE_COUNT;
  playBtn.disabled  = simPlaying || done;
  pauseBtn.disabled = !simPlaying;
  nextBtn.disabled  = simPlaying || done;
  prevBtn.disabled  = simPlaying || simCurrentStage <= 0;
}

function stepForward() {
  if (simCurrentStage >= STAGE_COUNT) {
    completeAllStages();
    simCurrentStage = STAGE_COUNT + 1;
    return;
  }
  simCurrentStage++;
  activateStage(simCurrentStage);
}

function stepBack() {
  if (simCurrentStage <= 0) return;
  simCurrentStage = Math.max(0, simCurrentStage - 1);
  if (simCurrentStage === 0) {
    resetSimulation();
  } else {
    // Re-activate previous stage
    for (let i = 1; i <= STAGE_COUNT; i++) {
      const node = getStageNode(i);
      const pill = getStagePill(i);
      const arrow = getArrow(i);
      if (i < simCurrentStage) {
        if (node) node.className = "pipeline-node is-done";
        if (pill) { pill.textContent = "Done"; pill.className = "node-status-pill done"; }
        if (arrow) arrow.className = "pipeline-arrow-line is-done";
      } else if (i === simCurrentStage) {
        if (node) node.className = "pipeline-node is-active";
        if (pill) { pill.textContent = "Running…"; pill.className = "node-status-pill active"; }
      } else {
        if (node) node.className = "pipeline-node is-pending";
        if (pill) { pill.textContent = "Pending"; pill.className = "node-status-pill pending"; }
        if (arrow) arrow.className = "pipeline-arrow-line";
      }
    }
    updateSimButtons();
  }
}

function startPlay() {
  if (simPlaying) return;
  simPlaying = true;
  updateSimButtons();
  simTimer = setInterval(() => {
    if (simCurrentStage >= STAGE_COUNT) {
      completeAllStages();
      simCurrentStage = STAGE_COUNT + 1;
      clearInterval(simTimer);
      simPlaying = false;
      updateSimButtons();
      return;
    }
    stepForward();
  }, getStageSeconds());
}

function pausePlay() {
  clearInterval(simTimer);
  simPlaying = false;
  updateSimButtons();
}

// Wire simulation buttons
["simPlayBtn",  "simPauseBtn", "simNextBtn",  "simPrevBtn",  "simResetBtn"].forEach((id, idx) => {
  const el = $(`#${id}`);
  if (!el) return;
  el.addEventListener("click", [startPlay, pausePlay, stepForward, stepBack, resetSimulation][idx]);
});

// ----------------------------------------------------------------
// Chunk Explorer
// ----------------------------------------------------------------
let cachedFilesList = [];

function populateChunkFileSelect(files) {
  cachedFilesList = files;
  const select = $("#chunkFileSelect");
  if (!select) return;
  const currentVal = select.value;
  select.innerHTML = '<option value="">-- Choose a committed file --</option>';
  for (const f of files) {
    const opt = document.createElement("option");
    opt.value = f.file_id;
    opt.textContent = `${f.name} (${formatBytes(f.size)})`;
    if (f.file_id === currentVal) opt.selected = true;
    select.append(opt);
  }
}

async function loadChunkExplorer(fileId) {
  if (!fileId) {
    $("#chunkExplorerMeta").style.display = "none";
    $("#chunkEmpty").hidden = false;
    $("#chunkTableWrap").hidden = true;
    $("#chunkCountPill").textContent = "-";
    return;
  }
  $("#chunkLoading").hidden = false;
  $("#chunkEmpty").hidden = true;
  $("#chunkTableWrap").hidden = true;

  try {
    const data = await api(`/api/files/${encodeURIComponent(fileId)}/chunks`);
    $("#chunkLoading").hidden = true;
    $("#chunkExplorerMeta").style.display = "flex";
    $("#chunkFileTotalSize").textContent = formatBytes(data.size);
    $("#chunkFileTotalChunks").textContent = Number(data.total_chunks).toLocaleString();
    $("#chunkCountPill").textContent = data.total_chunks.toLocaleString();

    const tbody = $("#chunkTableBody");
    tbody.replaceChildren();

    if (!data.chunks || !data.chunks.length) {
      $("#chunkEmpty").hidden = false;
      return;
    }

    $("#chunkTableWrap").hidden = false;

    data.chunks.forEach((chunk, idx) => {
      const row = document.createElement("tr");
      const shortHash = `${chunk.hash.slice(0, 10)}…${chunk.hash.slice(-8)}`;
      const isDup = chunk.is_duplicate;
      const isComp = chunk.codec && chunk.codec !== "none";
      
      let statusBadge = "";
      if (isDup) {
        statusBadge = `<span class="method-tag" style="background:var(--dusty-blue-l);color:#2D5E7A;">Reused (Ref: ${chunk.ref_count})</span>`;
      } else if (isComp) {
        statusBadge = `<span class="method-tag" style="background:var(--sage-l);color:#2D5E4E;">New · ${escapeHtml(chunk.codec)}</span>`;
      } else {
        statusBadge = `<span class="method-tag" style="background:var(--ivory);color:var(--ink-muted);">New · Raw</span>`;
      }

      const storageAction = isDup
        ? `<span style="color:var(--dusty-blue);font-weight:600;font-size:11.5px;">✓ Deduplicated (Zero S3 I/O)</span>`
        : `<span style="color:var(--terracotta);font-weight:600;font-size:11.5px;">S3 Object</span>`;

      row.innerHTML = `
        <td style="font-weight:700;color:var(--ink-muted);font-size:11px;">#${chunk.seq + 1}</td>
        <td><code style="font-size:11px;background:var(--ivory);padding:2px 6px;border-radius:4px;border:1px solid var(--border);" title="${escapeHtml(chunk.hash)}">${escapeHtml(shortHash)}</code></td>
        <td style="font-size:12px;color:var(--ink-muted);">${Number(chunk.offset).toLocaleString()} – ${Number(chunk.offset + chunk.length).toLocaleString()}</td>
        <td style="font-weight:600;">${formatBytes(chunk.length)}</td>
        <td><span style="font-size:11px;font-family:monospace;text-transform:uppercase;">${escapeHtml(chunk.codec || "none")}</span></td>
        <td>${statusBadge}</td>
        <td>${storageAction}</td>
      `;
      tbody.append(row);
    });
  } catch (err) {
    $("#chunkLoading").hidden = true;
    showGlobalError(`Failed to load chunks: ${err.message}`);
  }
}

$("#chunkFileSelect")?.addEventListener("change", (e) => {
  loadChunkExplorer(e.target.value);
});

// ----------------------------------------------------------------
// Storage Analytics Chart
// ----------------------------------------------------------------
let analyticsChartInstance = null;

async function renderAnalytics() {
  try {
    const stats = await api("/api/stats");
    const logical = Number(stats.logical_bytes) || 0;
    const physical = Number(stats.physical_bytes) || 0;
    const saved = Math.max(0, logical - physical);
    const ratio = Number(stats.dedup_ratio) || 1.0;
    const pct = Number(stats.storage_saved_pct) || 0.0;

    $("#analyticsEfficiency").textContent = `${pct.toFixed(1)}%`;
    $("#analyticsRatio").textContent = `${ratio.toFixed(2)}×`;
    $("#analyticsLogical").textContent = formatBytes(logical);
    $("#analyticsPhysical").textContent = formatBytes(physical);

    const canvas = $("#analyticsDoughnut");
    if (!canvas || typeof Chart === "undefined") return;

    if (analyticsChartInstance) {
      analyticsChartInstance.destroy();
    }

    analyticsChartInstance = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: ["Physical Storage Used", "Storage Space Saved"],
        datasets: [{
          data: [physical || 1, saved],
          backgroundColor: ["#263746", "#8FA69A"],
          borderColor: ["#FFFFFF", "#FFFFFF"],
          borderWidth: 2,
          hoverOffset: 6,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: "bottom",
            labels: { font: { family: "Inter", size: 12 }, padding: 16 }
          },
          tooltip: {
            callbacks: {
              label: (context) => ` ${context.label}: ${formatBytes(context.raw)}`
            }
          }
        },
        cutout: "68%"
      }
    });
  } catch (err) {
    console.error("Failed to render analytics", err);
  }
}

// ----------------------------------------------------------------
// File table actions (event delegation)
// ----------------------------------------------------------------
["filesBody", "filesBody2", "analyticsFilesBody"].forEach(bodyId => {
  const body = $(`#${bodyId}`);
  if (!body) return;
  body.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    const fileId = button.dataset.id;
    if (action === "download") {
      downloadFile(fileId, button);
      logAuditEvent("RESTORE", fileId, "File reconstructed from chunk store", "RESTORED");
    }
    if (action === "verify") {
      verifyFile(fileId, button);
      logAuditEvent("VERIFY", fileId, "SHA-256 integrity check validated", "VALIDATED");
    }
    if (action === "inspect") {
      activateSection("chunks");
      const sel = $("#chunkFileSelect");
      if (sel) { sel.value = fileId; loadChunkExplorer(fileId); }
    }
  });
});

// Activity filter pills
document.querySelectorAll(".act-filter-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    document.querySelectorAll(".act-filter-pill").forEach(p => p.classList.remove("is-active"));
    pill.classList.add("is-active");
    if (cachedFilesList) renderFullActivity(cachedFilesList);
  });
});
$("#activityRefreshBtn")?.addEventListener("click", () => {
  if (cachedFilesList) renderFullActivity(cachedFilesList);
});

// ----------------------------------------------------------------
// Upload event bindings
// ----------------------------------------------------------------
$("#chooseFile")?.addEventListener("click", () => fileInput.click());
fileInput?.addEventListener("change", () => selectFile(fileInput.files[0]));
uploadButton?.addEventListener("click", uploadSelectedFile);
$("#refreshButton")?.addEventListener("click", refreshAll);
["filesRefreshButton", "filesRefreshButton2"].forEach(id => {
  $(`#${id}`)?.addEventListener("click", refreshFiles);
});

dropZone?.addEventListener("click", (event) => { if (event.target === dropZone) fileInput.click(); });
["dragenter", "dragover"].forEach(ev => {
  dropZone?.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.add("is-over"); });
});
["dragleave", "drop"].forEach(ev => {
  dropZone?.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.remove("is-over"); });
});
dropZone?.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));

// When a file is uploaded, link the simulation nav
window.addEventListener("chunkwise:file-uploaded", () => {
  const simLink = $("#simulationLink");
  if (simLink) {
    simLink.onclick = (e) => {
      e.preventDefault();
      activateSection("simulation");
      resetSimulation();
    };
  }
});

// Simulation link in sidebar also resets
$$(".sidebar-nav a").forEach(a => {
  if (a.dataset.section === "simulation") {
    a.addEventListener("click", () => { resetSimulation(); });
  }
});

// ----------------------------------------------------------------
// Boot
// ----------------------------------------------------------------
navigateFromHash();
refreshAll();
