// Shared API client + app bootstrap/navigation. Loaded after setup.js and
// albums.js, but those only *reference* API/refreshFileList etc. from inside
// event handlers that fire later, so load order doesn't matter here.

let PAIRING_TOKEN = null;

const API = {
  async request(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (PAIRING_TOKEN) headers['X-Pairing-Token'] = PAIRING_TOKEN;
    let body = opts.body;
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.json);
    }
    const res = await fetch(path, { method: opts.method || 'GET', headers, body });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* no body */ }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  },

  async upload(path, file, extraFields = {}) {
    const form = new FormData();
    form.append('file', file);
    for (const [k, v] of Object.entries(extraFields)) {
      if (v !== undefined && v !== null) form.append(k, String(v));
    }
    const headers = {};
    if (PAIRING_TOKEN) headers['X-Pairing-Token'] = PAIRING_TOKEN;
    const res = await fetch(path, { method: 'POST', headers, body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* no body */ }
      throw new Error(detail);
    }
    return res.json();
  },
};

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let val = bytes;
  let unit = -1;
  do { val /= 1024; unit++; } while (val >= 1024 && unit < units.length - 1);
  return `${val.toFixed(1)} ${units[unit]}`;
}

function fileListRowHTML(file) {
  const date = new Date(file.uploaded_at * 1000).toLocaleString();
  return `
    <div class="list-row">
      <div style="flex:1;">
        <div class="list-row-title">${escapeHTML(file.filename)}</div>
        <div class="list-row-subtitle">${formatBytes(file.size)} &middot; ${date} &middot; ${file.source}</div>
      </div>
    </div>`;
}

function escapeHTML(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

async function refreshFileList(albumId, containerEl) {
  const url = albumId ? `/api/files?album_id=${albumId}` : '/api/files';
  const files = await API.request(url);
  containerEl.innerHTML = files.length
    ? files.map(fileListRowHTML).join('')
    : '<div class="list-row"><div class="list-row-subtitle">No files yet</div></div>';
}

function uploadRowHTML(jobId, filename) {
  return `
    <div class="upload-row" id="upload-row-${jobId}">
      <div class="upload-row-top">
        <span>${escapeHTML(filename)}</span>
        <span class="upload-row-status" id="upload-status-${jobId}">Starting&hellip;</span>
      </div>
      <div class="progress-bar"><div class="progress-bar-fill" id="upload-fill-${jobId}" style="width:0%"></div></div>
    </div>`;
}

async function pollUploadProgress(jobId, onDone) {
  const statusEl = document.getElementById(`upload-status-${jobId}`);
  const fillEl = document.getElementById(`upload-fill-${jobId}`);
  const tick = async () => {
    let job;
    try {
      job = await API.request(`/api/upload/${jobId}/progress`);
    } catch (e) {
      if (statusEl) { statusEl.textContent = e.message; statusEl.classList.add('error'); }
      return;
    }
    if (fillEl) fillEl.style.width = `${job.percent || 0}%`;
    if (statusEl) statusEl.textContent = job.status;
    if (job.status === 'error') {
      if (statusEl) { statusEl.textContent = job.error || 'Failed'; statusEl.classList.add('error'); }
      return;
    }
    if (job.status === 'done') {
      onDone();
      return;
    }
    setTimeout(tick, 700);
  };
  tick();
}

function handleUpload(file, albumId, uploadsContainerEl, fileListEl) {
  const rowId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  uploadsContainerEl.insertAdjacentHTML('afterbegin', uploadRowHTML(rowId, file.name));
  API.upload('/api/upload', file, { album_id: albumId })
    .then(({ job_id }) => {
      document.getElementById(`upload-row-${rowId}`).id = `upload-row-${job_id}`;
      document.getElementById(`upload-status-${rowId}`).id = `upload-status-${job_id}`;
      document.getElementById(`upload-fill-${rowId}`).id = `upload-fill-${job_id}`;
      pollUploadProgress(job_id, () => refreshFileList(albumId, fileListEl));
    })
    .catch((e) => {
      const statusEl = document.getElementById(`upload-status-${rowId}`);
      if (statusEl) { statusEl.textContent = e.message; statusEl.classList.add('error'); }
    });
}

function wireDropzone(dropzoneEl, browseBtnEl, fileInputEl, onFiles) {
  dropzoneEl.addEventListener('dragover', (e) => { e.preventDefault(); dropzoneEl.classList.add('dragover'); });
  dropzoneEl.addEventListener('dragleave', () => dropzoneEl.classList.remove('dragover'));
  dropzoneEl.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzoneEl.classList.remove('dragover');
    onFiles(e.dataTransfer.files);
  });
  browseBtnEl.addEventListener('click', () => fileInputEl.click());
  fileInputEl.addEventListener('change', () => onFiles(fileInputEl.files));
}

function initStorageView() {
  const dropzone = document.getElementById('dropzone-storage');
  const browseBtn = document.getElementById('btn-browse-storage');
  const fileInput = document.getElementById('file-input-storage');
  const uploads = document.getElementById('uploads-storage');
  const fileList = document.getElementById('file-list-storage');

  wireDropzone(dropzone, browseBtn, fileInput, (files) => {
    Array.from(files).forEach((f) => handleUpload(f, null, uploads, fileList));
  });
  refreshFileList(null, fileList);
}

function initSettingsView() {
  const watchFolderLabel = document.getElementById('current-watch-folder');
  const watchFolderInput = document.getElementById('input-watch-folder');
  const saveBtn = document.getElementById('btn-save-watch-folder');
  const tsToggle = document.getElementById('toggle-tailscale');
  const tokenBox = document.getElementById('pairing-token-box');
  const revealBtn = document.getElementById('btn-reveal-token');
  const rotateBtn = document.getElementById('btn-rotate-token');

  API.request('/api/settings').then((settings) => {
    watchFolderLabel.textContent = settings.watch_folder || 'Not set';
    watchFolderInput.value = settings.watch_folder || '';
    tsToggle.classList.toggle('on', !!settings.tailscale_access_enabled);
  });

  saveBtn.addEventListener('click', async () => {
    try {
      const { watch_folder } = await API.request('/api/settings/watch_folder', {
        method: 'PUT', json: { path: watchFolderInput.value || null },
      });
      watchFolderLabel.textContent = watch_folder || 'Not set';
    } catch (e) {
      alert(e.message);
    }
  });

  tsToggle.addEventListener('click', async () => {
    const enabled = !tsToggle.classList.contains('on');
    await API.request('/api/settings/tailscale_access', { method: 'PUT', json: { enabled } });
    tsToggle.classList.toggle('on', enabled);
  });

  revealBtn.addEventListener('click', async () => {
    tokenBox.textContent = await window.pywebview.api.get_pairing_token();
  });

  rotateBtn.addEventListener('click', async () => {
    if (!confirm('Rotate the pairing token? Any device using the old token (e.g. your iOS Shortcut) will need to be updated.')) return;
    tokenBox.textContent = await window.pywebview.api.rotate_pairing_token();
  });
}

function initNav() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('selected'));
      document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
      item.classList.add('selected');
      document.getElementById(`view-${item.dataset.view}`).classList.remove('hidden');
    });
  });
}

async function checkSetupStatus() {
  const { state } = await API.request('/api/setup/status');
  if (state === 'authorized') {
    document.getElementById('setup-overlay').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    initNav();
    initStorageView();
    initAlbumsView();
    initSettingsView();
  } else {
    document.getElementById('setup-overlay').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
    showWizardStep(state);
  }
}

function boot() {
  window.pywebview.api.get_pairing_token().then((token) => {
    PAIRING_TOKEN = token;
    checkSetupStatus();
  });
}

if (window.pywebview) {
  boot();
} else {
  window.addEventListener('pywebviewready', boot);
}
