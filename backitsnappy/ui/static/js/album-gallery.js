// Album drill-down view: a flat, edge-to-edge Photos.app-style thumbnail
// grid for one album's contents (the only gallery view now that Storage
// has been removed).
//
// Relies on API/escapeHTML/pollJobProgress/PAIRING_TOKEN/handleUpload/
// LIGHTBOX_OWNER/closeLightbox from app.js -- safe regardless of <script>
// load order, since those are only referenced from inside callbacks that
// run after every script has finished parsing.

const ALBUM_GALLERY = {
  albumId: null,
  files: [],
  lightboxIndex: -1,
  containerEl: null,
  selectMode: false,
  selectedIds: new Set(),
};

// Only one album can be actively uploading at a time -- the shared
// #uploads-album container belongs to whichever album started the batch,
// not to whichever album happens to be open right now. Switching away
// doesn't cancel it (it keeps running in the background), it just hides
// the now-irrelevant progress rows and shows a "wait for it" banner
// instead, and starting a *new* upload in a different album is blocked
// until the first one finishes -- avoids two albums' batches competing for
// the same backend throttling and confusing which progress belongs where.
let ACTIVE_UPLOAD_ALBUM = null; // { id, name } | null

// Which album's rows currently occupy #uploads-album -- distinct from
// ACTIVE_UPLOAD_ALBUM, which clears the moment the batch settles even
// though finished/failed rows are still sitting there. Without this, a
// failed upload from one album (e.g. moto videos) kept showing up while
// browsing into a completely different album (e.g. Kobe), since nothing
// ever cleared the shared container once the "upload in progress"
// scoping stopped applying.
let UPLOADS_ROWS_OWNER_ALBUM_ID = null;

function canStartUploadHere() {
  if (ACTIVE_UPLOAD_ALBUM && ACTIVE_UPLOAD_ALBUM.id !== ALBUM_GALLERY.albumId) {
    alert(
      `An upload is already in progress in "${ACTIVE_UPLOAD_ALBUM.name}". `
      + `Please wait for it to finish before starting a new one.`,
    );
    return false;
  }
  return true;
}

function beginAlbumUpload() {
  ACTIVE_UPLOAD_ALBUM = { id: ALBUM_GALLERY.albumId, name: CURRENT_ALBUM ? CURRENT_ALBUM.name : '' };
  UPLOADS_ROWS_OWNER_ALBUM_ID = ALBUM_GALLERY.albumId;
  updateUploadVisibility();
}

function endAlbumUpload() {
  ACTIVE_UPLOAD_ALBUM = null;
  updateUploadVisibility();
}

// Shows the real progress rows only when viewing the album an upload
// actually belongs to; any other album sees the "please wait" banner
// instead. Uploads keep running/updating in the background either way --
// this only toggles what's rendered, never destroys the row elements, so
// switching back shows their current state immediately.
function updateUploadVisibility() {
  const uploadsEl = document.getElementById('uploads-album');
  const banner = document.getElementById('upload-elsewhere-banner');
  if (ACTIVE_UPLOAD_ALBUM && ACTIVE_UPLOAD_ALBUM.id !== ALBUM_GALLERY.albumId) {
    uploadsEl.classList.add('hidden');
    banner.textContent = `Upload in progress in "${ACTIVE_UPLOAD_ALBUM.name}" — kindly wait till that completes.`;
    banner.classList.remove('hidden');
  } else {
    uploadsEl.classList.remove('hidden');
    banner.classList.add('hidden');
  }
}

function albumFileIconHTML(file) {
  const ext = (file.filename.split('.').pop() || 'file').toUpperCase().slice(0, 5);
  return `<div class="gallery-cell-file"><div class="gallery-cell-file-ext">${escapeHTML(ext)}</div></div>`;
}

function albumCellHTML(file, index) {
  const mediaHTML = file.has_thumbnail
    ? `<img src="/api/files/${file.id}/thumbnail?token=${encodeURIComponent(PAIRING_TOKEN)}" loading="lazy" draggable="false" alt="${escapeHTML(file.filename)}">`
    : albumFileIconHTML(file);
  const selected = ALBUM_GALLERY.selectedIds.has(file.id);
  return `
    <div class="album-cell${selected ? ' selected' : ''}" data-file-id="${file.id}" data-index="${index}">
      ${mediaHTML}
      ${file.media_type === 'video' ? `<div class="gallery-cell-badge">${ICONS.play}</div>` : ''}
      <div class="gallery-cell-overlay glass-material-thin">
        <button class="gallery-cell-icon-btn" data-action="download" title="Download">${ICONS.download}</button>
        <button class="gallery-cell-icon-btn" data-action="delete" title="Delete">${ICONS.trash}</button>
      </div>
      <div class="gallery-cell-checkbox${ALBUM_GALLERY.selectMode ? '' : ' hidden'}" data-action="select"></div>
    </div>`;
}

function renderAlbumGallery() {
  const html = ALBUM_GALLERY.files.map((f, i) => albumCellHTML(f, i)).join('');
  ALBUM_GALLERY.containerEl.innerHTML = html
    || `<div class="gallery-empty">
         <div class="icon">⬆︎</div>
         <div>Drag photos here to add them, or use the &plus; button above.</div>
       </div>`;
  wireAlbumCellEvents();
  updateSelectButtonVisibility();
}

// No point offering "Select" over an empty album -- and if it somehow
// became empty while already in select mode (e.g. the last file got
// deleted), fall out of select mode too rather than leaving it stranded.
function updateSelectButtonVisibility() {
  const hasFiles = ALBUM_GALLERY.files.length > 0;
  document.getElementById('album-btn-select-mode').classList.toggle('hidden', !hasFiles);
  if (!hasFiles && ALBUM_GALLERY.selectMode) {
    setAlbumSelectMode(false);
  }
}

function wireAlbumCellEvents() {
  ALBUM_GALLERY.containerEl.querySelectorAll('.album-cell').forEach((cell) => {
    const fileId = Number(cell.dataset.fileId);
    const index = Number(cell.dataset.index);
    cell.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="download"]')) {
        e.stopPropagation();
        albumQuickDownload(fileId);
        return;
      }
      if (e.target.closest('[data-action="delete"]')) {
        e.stopPropagation();
        albumConfirmDeleteFile(fileId);
        return;
      }
      if (ALBUM_GALLERY.selectMode || e.target.closest('[data-action="select"]')) {
        e.stopPropagation();
        toggleAlbumSelect(fileId, cell);
        return;
      }
      openAlbumLightbox(index);
    });
  });
}

function toggleAlbumSelect(fileId, cellEl) {
  if (ALBUM_GALLERY.selectedIds.has(fileId)) {
    ALBUM_GALLERY.selectedIds.delete(fileId);
    cellEl.classList.remove('selected');
  } else {
    ALBUM_GALLERY.selectedIds.add(fileId);
    cellEl.classList.add('selected');
  }
  updateAlbumSelectionToolbar();
}

function updateAlbumSelectionToolbar() {
  const n = ALBUM_GALLERY.selectedIds.size;
  const dlBtn = document.getElementById('album-btn-download-selected');
  const delBtn = document.getElementById('album-btn-delete-selected');
  dlBtn.textContent = `Download ${n}`;
  dlBtn.disabled = n === 0;
  delBtn.textContent = `Delete ${n}`;
  delBtn.disabled = n === 0;
}

function setAlbumSelectMode(active) {
  ALBUM_GALLERY.selectMode = active;
  if (!active) ALBUM_GALLERY.selectedIds.clear();
  document.getElementById('album-btn-select-mode').textContent = active ? 'Cancel' : 'Select';
  document.getElementById('album-btn-select-all').classList.toggle('hidden', !active);
  document.getElementById('album-btn-download-selected').classList.toggle('hidden', !active);
  document.getElementById('album-btn-delete-selected').classList.toggle('hidden', !active);
  document.getElementById('btn-add-photos').classList.toggle('hidden', active);
  updateAlbumSelectionToolbar();
  renderAlbumGallery();
}

function selectAllAlbumFiles() {
  ALBUM_GALLERY.files.forEach((f) => ALBUM_GALLERY.selectedIds.add(f.id));
  updateAlbumSelectionToolbar();
  renderAlbumGallery();
}

async function downloadSelectedAlbumFiles() {
  for (const id of Array.from(ALBUM_GALLERY.selectedIds)) {
    try {
      const { job_id } = await API.request(`/api/files/${id}/download`, {
        method: 'POST', json: { destination: 'default' },
      });
      showAlbumDownloadToast(job_id, id);
    } catch (e) {
      alert(`${e.message} (file ${id})`);
    }
  }
}

const BULK_DELETE_LIMIT = 10;

async function deleteSelectedAlbumFiles() {
  const n = ALBUM_GALLERY.selectedIds.size;
  if (!n) return;
  if (n > BULK_DELETE_LIMIT) {
    alert(
      'For more than 10 items, delete them directly in Telegram, then press "Sync with Telegram" '
      + 'in Settings to update the local index.',
    );
    return;
  }
  if (!confirm(`Delete ${n} file${n === 1 ? '' : 's'}? This can't be undone.`)) return;
  for (const id of Array.from(ALBUM_GALLERY.selectedIds)) {
    try {
      await API.request(`/api/files/${id}`, { method: 'DELETE' });
    } catch (e) {
      alert(`${e.message} (file ${id})`);
    }
  }
  setAlbumSelectMode(false);
  await refreshAlbumGallery();
  runSync({ toast: true });
}

function albumDownloadRowHTML(jobId, filename) {
  return `
    <div class="download-row glass-material-thin" id="download-row-${jobId}">
      <button class="row-close-btn" id="download-cancel-${jobId}" title="Cancel download" aria-label="Cancel download">&times;</button>
      <div class="download-row-filename" title="${escapeHTML(filename)}">${escapeHTML(filename)}</div>
      <div class="upload-row-status" id="download-status-${jobId}">Starting&hellip;</div>
      <div class="progress-bar"><div class="progress-bar-fill" id="download-fill-${jobId}" style="width:0%"></div></div>
    </div>`;
}

function showAlbumDownloadToast(jobId, fileId) {
  const panel = document.getElementById('downloads-panel');
  panel.classList.remove('hidden');
  const file = ALBUM_GALLERY.files.find((f) => f.id === fileId);
  panel.insertAdjacentHTML('afterbegin', albumDownloadRowHTML(jobId, file ? file.filename : 'file'));
  const statusEl = document.getElementById(`download-status-${jobId}`);
  const fillEl = document.getElementById(`download-fill-${jobId}`);
  const closeBtn = document.getElementById(`download-cancel-${jobId}`);

  closeBtn.addEventListener('click', () => {
    if (closeBtn.dataset.mode === 'dismiss') {
      document.getElementById(`download-row-${jobId}`)?.remove();
      return;
    }
    closeBtn.disabled = true;
    API.request(`/api/files/${fileId}/download/${jobId}/cancel`, { method: 'POST' }).catch(() => {});
  });

  pollJobProgress(`/api/files/${fileId}/download/${jobId}/progress`, (job) => {
    if (fillEl) fillEl.style.width = `${job.percent || 0}%`;
    if (statusEl) statusEl.textContent = job.status;
  })
    .then(() => {
      if (statusEl) statusEl.textContent = 'Saved';
      closeBtn.remove();
      setTimeout(() => document.getElementById(`download-row-${jobId}`)?.remove(), 4000);
    })
    .catch((e) => {
      if (statusEl) { statusEl.textContent = e.message; statusEl.classList.add('error'); }
      if (e.message === 'Cancelled') {
        closeBtn.remove();
        setTimeout(() => document.getElementById(`download-row-${jobId}`)?.remove(), 4000);
      } else {
        closeBtn.disabled = false;
        closeBtn.dataset.mode = 'dismiss';
        closeBtn.title = 'Dismiss';
        closeBtn.setAttribute('aria-label', 'Dismiss');
      }
    });
}

async function albumQuickDownload(fileId) {
  try {
    const { job_id } = await API.request(`/api/files/${fileId}/download`, {
      method: 'POST', json: { destination: 'default' },
    });
    showAlbumDownloadToast(job_id, fileId);
  } catch (e) {
    alert(e.message);
  }
}

async function albumSaveAs(file) {
  const path = await window.pywebview.api.save_file_dialog(file.filename);
  if (!path) return;
  try {
    const { job_id } = await API.request(`/api/files/${file.id}/download`, {
      method: 'POST', json: { destination: 'custom', path },
    });
    showAlbumDownloadToast(job_id, file.id);
  } catch (e) {
    alert(e.message);
  }
}

async function albumConfirmDeleteFile(fileId) {
  const file = ALBUM_GALLERY.files.find((f) => f.id === fileId);
  if (!confirm(`Delete "${file ? file.filename : 'this file'}"? This can't be undone.`)) return;
  try {
    await API.request(`/api/files/${fileId}`, { method: 'DELETE' });
    if (ALBUM_GALLERY.files[ALBUM_GALLERY.lightboxIndex]?.id === fileId) {
      closeLightbox();
    }
    await refreshAlbumGallery();
    runSync({ toast: true });
  } catch (e) {
    alert(e.message);
  }
}

function openAlbumLightbox(index) {
  LIGHTBOX_OWNER = {
    lightboxPrev: _albumLightboxPrev,
    lightboxNext: _albumLightboxNext,
    cancelActivePrepare: _albumCancelActivePrepare,
  };
  ALBUM_GALLERY.lightboxIndex = index;
  document.getElementById('lightbox-overlay').classList.remove('hidden');
  showAlbumLightboxFile();
}

// Tracks whichever prepare (download-for-viewing) job is currently in
// flight for the open lightbox, so closing it or navigating to a
// different file can cancel a still-running download instead of letting
// it keep pulling the full file in the background for nothing.
let ACTIVE_PREPARE = null; // { fileId, jobId } | null

function _albumCancelActivePrepare() {
  if (!ACTIVE_PREPARE || !ACTIVE_PREPARE.jobId) {
    ACTIVE_PREPARE = null;
    return;
  }
  const { fileId, jobId } = ACTIVE_PREPARE;
  ACTIVE_PREPARE = null;
  // Best-effort, fire-and-forget -- a job that already finished (or
  // whose file was deleted) 404s here, which is fine to ignore.
  API.request(`/api/files/${fileId}/download/${jobId}/cancel`, { method: 'POST' }).catch(() => {});
}

// Prefixed, not `lightboxPrev`/`lightboxNext` -- see gallery.js's comment
// on _storageLightboxPrev for why (plain global functions, not closures).
function _albumLightboxPrev() {
  if (ALBUM_GALLERY.lightboxIndex > 0) { ALBUM_GALLERY.lightboxIndex--; showAlbumLightboxFile(); }
}

function _albumLightboxNext() {
  if (ALBUM_GALLERY.lightboxIndex < ALBUM_GALLERY.files.length - 1) {
    ALBUM_GALLERY.lightboxIndex++;
    showAlbumLightboxFile();
  }
}

function showAlbumLightboxFile() {
  const file = ALBUM_GALLERY.files[ALBUM_GALLERY.lightboxIndex];
  if (!file) return;
  _albumCancelActivePrepare(); // navigating away from whichever file was still downloading
  document.getElementById('lightbox-title').textContent = file.filename;
  const mediaEl = document.getElementById('lightbox-media');

  // Video streams straight from Telegram via ranged reads -- no /prepare,
  // no full download, no local cache write. Playback starts immediately
  // and seeking just issues new Range requests, same as any HTML5 video.
  // Images stay on /prepare + /media: they're small enough that a full
  // local copy is cheap, and it means the <img> keeps working if you
  // reopen it without needing the network again.
  if (file.media_type === 'video') {
    const url = `/api/files/${file.id}/stream?token=${encodeURIComponent(PAIRING_TOKEN)}`;
    // Starts muted and stays muted -- autoplaying with sound on every
    // video someone opens is more surprising than welcome; the native
    // controls' mute button is right there for anyone who wants sound.
    mediaEl.innerHTML = `<video src="${url}" controls autoplay muted playsinline></video>`;
    const videoEl = mediaEl.querySelector('video');
    // The bare `autoplay` attribute alone doesn't reliably start playback
    // inside pywebview's embedded WKWebView on macOS -- unlike a real
    // Safari/Chrome tab, it doesn't always treat an autoplay-attribute
    // video as exempt from its "needs a user action" media policy. An
    // explicit .play() call does count as that user action, but only if
    // it runs synchronously inside the click handler that opened this
    // lightbox (the call chain here is: gallery cell click ->
    // openAlbumLightbox -> showAlbumLightboxFile, all synchronous) -- if
    // it were deferred behind a promise/await first, WebKit would no
    // longer consider it gesture-triggered and would silently ignore it.
    videoEl.play().catch(() => {});
    // No separate thumbnail request needed here anymore -- /stream itself
    // opportunistically captures a thumbnail from the same bytes it's
    // fetching for playback (see routes_media.py), so opening the video
    // is all it takes.
  } else {
    mediaEl.innerHTML = '<div class="lightbox-loading">Loading&hellip;</div>';
    API.request(`/api/files/${file.id}/prepare`, { method: 'POST' })
      .then(({ job_id }) => {
        if (ALBUM_GALLERY.files[ALBUM_GALLERY.lightboxIndex] === file) {
          ACTIVE_PREPARE = { fileId: file.id, jobId: job_id };
        }
        return pollJobProgress(
          `/api/files/${file.id}/download/${job_id}/progress`,
          (job) => {
            const el = mediaEl.querySelector('.lightbox-loading');
            if (el) el.textContent = `Loading… ${job.percent || 0}%`;
          },
        );
      })
      .then(() => {
        ACTIVE_PREPARE = null; // finished -- nothing left to cancel
        if (ALBUM_GALLERY.files[ALBUM_GALLERY.lightboxIndex] !== file) return; // navigated away
        const url = `/api/files/${file.id}/media?token=${encodeURIComponent(PAIRING_TOKEN)}`;
        mediaEl.innerHTML = `<img src="${url}" alt="${escapeHTML(file.filename)}">`;
      })
      .catch((e) => {
        ACTIVE_PREPARE = null; // errored (or was cancelled) -- nothing left to cancel
        if (ALBUM_GALLERY.files[ALBUM_GALLERY.lightboxIndex] !== file) return; // navigated away
        mediaEl.innerHTML = `<div class="lightbox-error">${escapeHTML(e.message)}</div>`;
      });
  }

  document.getElementById('btn-lightbox-download').onclick = () => albumQuickDownload(file.id);
  document.getElementById('btn-lightbox-save-as').onclick = () => albumSaveAs(file);
  const deleteBtn = document.getElementById('btn-lightbox-delete');
  deleteBtn.classList.remove('hidden'); // albums always allow delete
  deleteBtn.onclick = () => albumConfirmDeleteFile(file.id);
}

async function refreshAlbumGallery() {
  if (ALBUM_GALLERY.albumId == null) return;
  ALBUM_GALLERY.files = await API.request(`/api/files?album_id=${ALBUM_GALLERY.albumId}`);
  renderAlbumGallery();
}

function openAlbumGallery(albumId) {
  ALBUM_GALLERY.albumId = albumId;
  document.getElementById('invite-result').innerHTML = '';
  document.getElementById('invite-popover').classList.add('hidden');
  if (ALBUM_GALLERY.selectMode) setAlbumSelectMode(false);
  // Opening a different album than whichever one's rows are sitting in
  // #uploads-album (and no upload is actively in progress for it) --
  // those rows are stale/finished, don't carry them into a new album.
  if (UPLOADS_ROWS_OWNER_ALBUM_ID !== null && UPLOADS_ROWS_OWNER_ALBUM_ID !== albumId && !ACTIVE_UPLOAD_ALBUM) {
    document.getElementById('uploads-album').innerHTML = '';
    UPLOADS_ROWS_OWNER_ALBUM_ID = null;
  }
  updateUploadVisibility();
  refreshAlbumGallery();
}

function initAlbumGallery() {
  ALBUM_GALLERY.containerEl = document.getElementById('album-gallery-grid');
  const uploads = document.getElementById('uploads-album');
  const grid = ALBUM_GALLERY.containerEl;

  // Drag-and-drop straight onto the grid -- this view has no separate
  // upload dropzone by design (Photos.app-style: the grid itself is it).
  grid.addEventListener('dragover', (e) => { e.preventDefault(); grid.classList.add('dragover'); });
  grid.addEventListener('dragleave', () => grid.classList.remove('dragover'));
  grid.addEventListener('drop', async (e) => {
    e.preventDefault();
    grid.classList.remove('dragover');
    if (ALBUM_GALLERY.albumId == null) return;
    if (!canStartUploadHere()) return;
    const files = filterValidDroppedFiles(e.dataTransfer.files);
    if (!files.length) return;
    beginAlbumUpload();
    await queueBatchUpload(files, ALBUM_GALLERY.albumId, uploads, refreshAlbumGallery);
    endAlbumUpload();
  });

  // Small "+" control in the finder toolbar (swapped in for "+ New Folder"
  // once inside an album -- see albums.js) as the alternate, click-driven
  // way to add photos, per the request for an "unobtrusive add control."
  const addBtn = document.getElementById('btn-add-photos');
  const fileInput = document.getElementById('file-input-album');
  addBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    if (ALBUM_GALLERY.albumId == null) return;
    if (!canStartUploadHere()) { fileInput.value = ''; return; }
    beginAlbumUpload();
    // Don't reset .value until the whole batch has actually finished --
    // see queueBatchUpload's comment on the WebKit file-input bug this
    // works around. Resetting it early is exactly what silently broke
    // large batches (750 files, none uploaded, "field required" on all).
    await queueBatchUpload(fileInput.files, ALBUM_GALLERY.albumId, uploads, refreshAlbumGallery);
    fileInput.value = '';
    endAlbumUpload();
  });

  // Multi-select: toggle mode, select all, bulk download/delete.
  document.getElementById('album-btn-select-mode').addEventListener('click', () => {
    setAlbumSelectMode(!ALBUM_GALLERY.selectMode);
  });
  document.getElementById('album-btn-select-all').addEventListener('click', selectAllAlbumFiles);
  document.getElementById('album-btn-download-selected').addEventListener('click', downloadSelectedAlbumFiles);
  document.getElementById('album-btn-delete-selected').addEventListener('click', deleteSelectedAlbumFiles);

  // Cmd/Ctrl+A selects all files in the open album -- entering select mode
  // first if it wasn't already active, matching Finder's own behavior.
  document.addEventListener('keydown', (e) => {
    if (document.getElementById('album-drilldown-view').classList.contains('hidden')) return;
    if (document.activeElement && ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      if (!ALBUM_GALLERY.selectMode) setAlbumSelectMode(true);
      selectAllAlbumFiles();
    }
  });

  // Floating invite control: a small FAB that reveals a popover, rather
  // than a permanent inline field cluttering the layout.
  const fab = document.getElementById('invite-fab');
  const popover = document.getElementById('invite-popover');
  fab.addEventListener('click', (e) => {
    e.stopPropagation();
    popover.classList.toggle('hidden');
  });
  document.addEventListener('click', (e) => {
    if (!popover.classList.contains('hidden') && !popover.contains(e.target) && e.target !== fab) {
      popover.classList.add('hidden');
    }
  });

  const inviteBtn = document.getElementById('btn-invite');
  const inviteInput = document.getElementById('input-invite-username');
  inviteBtn.addEventListener('click', async () => {
    if (ALBUM_GALLERY.albumId == null) return;
    const username = inviteInput.value.trim();
    if (!username) return;
    const resultEl = document.getElementById('invite-result');
    inviteBtn.disabled = true;
    resultEl.textContent = 'Inviting…';
    try {
      const result = await API.request(`/api/albums/${ALBUM_GALLERY.albumId}/invite`, {
        method: 'POST', json: { username },
      });
      if (result.method === 'direct') {
        resultEl.textContent = `Added @${username} directly.`;
      } else {
        resultEl.innerHTML = `@${escapeHTML(username)} couldn't be added directly (their privacy settings block it) — share this invite link instead:
          <div class="invite-link-box">${escapeHTML(result.invite_link)}</div>`;
      }
      inviteInput.value = '';
    } catch (e) {
      resultEl.textContent = e.message;
    } finally {
      inviteBtn.disabled = false;
    }
  });
}
