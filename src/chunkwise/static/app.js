"use strict";

const $ = (selector) => document.querySelector(selector);
const fileInput = $("#fileInput");
const dropZone = $("#dropZone");
const uploadButton = $("#uploadButton");
let selectedFile = null;
let pendingUploadKey = null;
let pendingUploadFingerprint = null;
let refreshGeneration = 0;

function formatBytes(value) {
  if (value === 0) return "0 B";
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** index);
  return `${amount.toFixed(index === 0 || amount >= 100 ? 0 : 1)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.blob();
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

function uploadKeyStorageKey(fingerprint) {
  return `chunkwise-pending-upload:${fingerprint}`;
}

async function refreshStats() {
  const stats = await api("/api/stats");
  $("#statLogical").textContent = formatBytes(stats.logical_bytes);
  $("#statPhysical").textContent = formatBytes(stats.physical_bytes);
  $("#statRatio").textContent = `${Number(stats.dedup_ratio).toFixed(2)}×`;
  $("#statSaved").textContent = `${Number(stats.storage_saved_pct).toFixed(1)}%`;
  $("#statSavedDetail").textContent = `${formatBytes(Math.max(0, stats.logical_bytes - stats.physical_bytes))} less stored`;
  $("#statFiles").textContent = Number(stats.files).toLocaleString();
  $("#statChunks").textContent = Number(stats.unique_chunks).toLocaleString();
  $("#compositionLogical").textContent = formatBytes(stats.logical_bytes);
  $("#compositionPhysical").textContent = formatBytes(stats.physical_bytes);
  $("#compositionSaved").textContent = formatBytes(Math.max(0, stats.logical_bytes - stats.physical_bytes));
  const storedShare = stats.logical_bytes > 0 ? Math.min(100, (stats.physical_bytes / stats.logical_bytes) * 100) : 0;
  $("#compositionBar").style.width = `${storedShare}%`;
  $("#compositionNote").textContent = stats.files
    ? `${Number(stats.unique_chunks).toLocaleString()} unique chunks across ${Number(stats.files).toLocaleString()} committed files.`
    : "Storage totals will appear when data is available.";
  $("#statsGrid").setAttribute("aria-busy", "false");
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

function renderFiles(files) {
  const body = $("#filesBody");
  body.replaceChildren();
  $("#fileCount").textContent = files.length.toLocaleString();
  if (!files.length) {
    $("#filesEmpty").hidden = false;
    $("#filesTableWrap").hidden = true;
    return;
  }
  $("#filesEmpty").hidden = true;
  $("#filesTableWrap").hidden = false;
  for (const file of files) {
    const row = document.createElement("tr");
    const safeName = escapeHtml(file.name);
    const extension = (file.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
    row.innerHTML = `<td><div class="file-name"><span class="file-glyph">${escapeHtml(extension)}</span><span title="${safeName}">${safeName}</span></div></td>
      <td>${formatBytes(file.size)}</td><td><span class="method-tag">${escapeHtml(file.chunker)}</span></td>
      <td>${escapeHtml(dateLabel(file.created_at))}</td><td><span class="file-id">${escapeHtml(file.file_id)}</span></td>
      <td><div class="row-actions"><button class="action-button" data-action="verify" data-id="${escapeHtml(file.file_id)}">Verify</button><button class="action-button" data-action="download" data-id="${escapeHtml(file.file_id)}">Restore</button></div></td>`;
    body.append(row);
  }
}

async function refreshFiles() {
  const generation = ++refreshGeneration;
  $("#filesLoading").hidden = false;
  $("#filesTableWrap").hidden = true;
  $("#filesEmpty").hidden = true;
  $("#filesError").hidden = true;
  try {
    const result = await api("/api/files");
    if (generation === refreshGeneration) renderFiles(result.files);
  } catch (error) {
    if (generation === refreshGeneration) {
      $("#filesError").textContent = error.message;
      $("#filesError").hidden = false;
    }
  } finally {
    if (generation === refreshGeneration) $("#filesLoading").hidden = true;
  }
}

async function refreshAll() {
  $("#globalError").hidden = true;
  try {
    const [, , , config] = await Promise.all([
      refreshStats(), refreshFiles(), api("/api/health"), api("/api/config"),
    ]);
    $("#uploadLimit").textContent = `Maximum upload size: ${formatBytes(config.max_upload_bytes)}`;
    $("#connectionLabel").textContent = "Storage online";
    $("#connectionStatus").classList.remove("is-offline");
  } catch (error) {
    $("#statsGrid").setAttribute("aria-busy", "false");
    $("#connectionLabel").textContent = "Storage offline";
    $("#connectionStatus").classList.add("is-offline");
    showGlobalError(`Storage could not be loaded: ${error.message}`);
  }
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
  $("#selectedFile").textContent = `${file.name} · ${formatBytes(file.size)}`;
  $("#selectedFile").hidden = false;
  uploadButton.disabled = false;
  $("#uploadMessage").hidden = true;
}

function uploadSelectedFile() {
  if (!selectedFile) return;
  const file = selectedFile;
  pendingUploadKey ||= crypto.randomUUID();
  try { sessionStorage.setItem(uploadKeyStorageKey(pendingUploadFingerprint), pendingUploadKey); }
  catch { /* The in-memory key still protects retries in this page. */ }
  const chunker = document.querySelector('input[name="chunker"]:checked').value;
  const progress = $("#uploadProgress");
  progress.hidden = false;
  uploadButton.disabled = true;
  $("#uploadMessage").hidden = true;
  $("#progressLabel").textContent = "Uploading and indexing";
  $("#progressPercent").textContent = "0%";
  $("#progressBar").style.width = "0%";
  const request = new XMLHttpRequest();
  request.open("POST", "/api/files");
  request.setRequestHeader("Content-Type", "application/octet-stream");
  request.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
  request.setRequestHeader("X-Chunker", chunker);
  request.setRequestHeader("Idempotency-Key", pendingUploadKey);
  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
    $("#progressPercent").textContent = `${percent}%`;
    $("#progressBar").style.width = `${percent}%`;
  });
  request.addEventListener("load", async () => {
    uploadButton.disabled = false;
    progress.hidden = true;
    let response;
    try { response = JSON.parse(request.responseText); } catch { response = {}; }
    if (request.status < 200 || request.status >= 300) {
      setUploadMessage(response.error || `Upload failed (${request.status})`, "error");
      return;
    }
    selectedFile = null;
    try { sessionStorage.removeItem(uploadKeyStorageKey(pendingUploadFingerprint)); }
    catch { /* Storage may be disabled; the upload itself has completed. */ }
    pendingUploadKey = null;
    pendingUploadFingerprint = null;
    fileInput.value = "";
    $("#selectedFile").hidden = true;
    uploadButton.disabled = true;
    setUploadMessage(`${response.name} stored · ${Number(response.chunks).toLocaleString()} chunks · ${formatBytes(response.new_bytes)} new bytes`, "success");
    await refreshAll();
  });
  request.addEventListener("error", () => {
    uploadButton.disabled = false;
    progress.hidden = true;
    setUploadMessage("Network error. Retry this file; the same request key prevents duplicate records.", "error");
  });
  request.send(file);
}

async function downloadFile(fileId, button) {
  button.disabled = true;
  try {
    const blob = await api(`/api/files/${encodeURIComponent(fileId)}/download`);
    const row = button.closest("tr");
    const filename = row.querySelector(".file-name span:last-child").textContent;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
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
    button.textContent = result.verified ? "Verified" : "Failed";
    button.title = `${formatBytes(result.size)} verified`;
  } catch (error) {
    button.textContent = "Error";
    button.title = error.message;
  } finally {
    window.setTimeout(() => { button.textContent = original; button.disabled = false; }, 2200);
  }
}

$("#chooseFile").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => selectFile(fileInput.files[0]));
uploadButton.addEventListener("click", uploadSelectedFile);
$("#refreshButton").addEventListener("click", refreshAll);
$("#filesRefreshButton").addEventListener("click", refreshFiles);
dropZone.addEventListener("click", (event) => { if (event.target === dropZone) fileInput.click(); });
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add("is-over"); });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove("is-over"); });
}
dropZone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
$("#filesBody").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "download") downloadFile(button.dataset.id, button);
  if (button.dataset.action === "verify") verifyFile(button.dataset.id, button);
});

refreshAll();