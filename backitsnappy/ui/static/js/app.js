// Shared API client + app bootstrap/navigation, plus the shared lightbox
// chrome (one overlay in the DOM, used by album-gallery.js -- previously
// also by gallery.js/Storage before that tab was removed). Loaded after
// setup.js, albums.js and album-gallery.js, but those only *reference*
// API/escapeHTML/LIGHTBOX_OWNER/closeLightbox/etc. from inside event
// handlers that fire later, so load order doesn't matter here.

let PAIRING_TOKEN = null;
let LIGHTBOX_OWNER = null; // whichever gallery module currently owns the open lightbox

// Inline SVG icon set, monochrome via currentColor so each usage site's own
// color rules (nav selected state, white icons over photo thumbnails, etc.)
// apply automatically with no extra CSS. Replaces the emoji glyphs the UI
// started with -- consistent stroke weight reads as a native, considered
// icon set rather than a grab-bag of platform emoji renderings.
const ICONS = {
  albums: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="5" y="3" width="12" height="12" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M3 7v8a2 2 0 0 0 2 2h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  settings: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="2.5" stroke="currentColor" stroke-width="1.5"/><path d="M10 2.5v2M10 15.5v2M17.5 10h-2M4.5 10h-2M15.36 4.64l-1.41 1.41M6.05 13.95l-1.41 1.41M15.36 15.36l-1.41-1.41M6.05 6.05L4.64 4.64" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  folder: '<svg class="icon-svg icon-svg-lg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6a1.5 1.5 0 0 1 1.2.6l.9 1.2a1.5 1.5 0 0 0 1.2.6h4.1A1.5 1.5 0 0 1 17 8v6.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5v-9Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
  iphone: '<svg class="icon-svg icon-svg-lg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="8" height="16" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M9 15.5h2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  invite: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="7" r="3" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 17c.6-3 2.8-5 5.5-5s4.9 2 5.5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M15.5 5.5v5M13 8h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  download: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M10 3v9m0 0-3.5-3.5M10 12l3.5-3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.5 14.5v1a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  trash: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 6h12M8 6V4.5A1.5 1.5 0 0 1 9.5 3h1A1.5 1.5 0 0 1 12 4.5V6M6 6v9a1.5 1.5 0 0 0 1.5 1.5h5A1.5 1.5 0 0 0 14 15V6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M8.5 9v4M11.5 9v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  play: '<svg class="icon-svg" viewBox="0 0 20 20" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M6.5 4.8c0-1 1.1-1.6 2-1.1l7 4.2c.9.5.9 1.8 0 2.3l-7 4.2c-.9.5-2-.1-2-1.1V4.8Z"/></svg>',
  chevronLeft: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12.5 5 7.5 10l5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chevronRight: '<svg class="icon-svg" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7.5 5 12.5 10l-5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

// FastAPI's error body is `{ detail: "..." }` for our own HTTPExceptions,
// but `{ detail: [{loc, msg, type}, ...] }` (a list, not a string) for
// Pydantic/param validation failures (422s). Passing that list straight to
// `new Error(detail)` silently stringifies it to "[object Object]" instead
// of throwing -- this always extracts an actual readable string.
async function extractErrorDetail(res) {
  let detail = res.statusText;
  try {
    const body = await res.json();
    if (typeof body.detail === 'string') {
      detail = body.detail;
    } else if (Array.isArray(body.detail)) {
      detail = body.detail.map((d) => d.msg || JSON.stringify(d)).join('; ') || detail;
    } else if (body.detail) {
      detail = JSON.stringify(body.detail);
    }
  } catch (e) { /* no body, or not JSON */ }
  return detail;
}

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
      throw new Error(await extractErrorDetail(res));
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
      throw new Error(await extractErrorDetail(res));
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

function formatSyncResult(result) {
  const parts = [];
  if (result.added > 0) parts.push(`imported ${result.added} new file${result.added === 1 ? '' : 's'}`);
  if (result.removed > 0) parts.push(`removed ${result.removed} stale entr${result.removed === 1 ? 'y' : 'ies'}`);
  const channelsNote = `${result.channels_checked} channel${result.channels_checked === 1 ? '' : 's'} checked`;
  return parts.length ? `${parts.join(', ')} (${channelsNote}).` : `Everything's already in sync (${channelsNote}).`;
}

let syncToastHideTimer = null;
function showSyncToast(message, { autoHide = false, isError = false } = {}) {
  const toast = document.getElementById('sync-toast');
  if (!toast) return;
  clearTimeout(syncToastHideTimer);
  toast.textContent = message;
  toast.classList.remove('hidden');
  toast.classList.toggle('error', !!isError);
  if (autoHide) {
    syncToastHideTimer = setTimeout(() => toast.classList.add('hidden'), 4000);
  }
}

// Shared by the manual "Sync with Telegram" button (Settings) and every
// automatic trigger (app launch, after a small delete) -- toast:true shows
// a lightweight non-blocking notification instead of writing into the
// (possibly not currently visible) Settings page.
async function runSync({ toast = false } = {}) {
  const resultEl = document.getElementById('sync-result');
  if (toast) showSyncToast('Syncing with Telegram…');
  else if (resultEl) resultEl.textContent = 'Syncing…';
  try {
    const result = await API.request('/api/settings/sync', { method: 'POST' });
    const message = formatSyncResult(result);
    if (toast) showSyncToast(message, { autoHide: true });
    if (resultEl) resultEl.textContent = message;
    refreshAlbumGallery();
    return result;
  } catch (e) {
    if (toast) showSyncToast(e.message, { autoHide: true, isError: true });
    if (resultEl) resultEl.textContent = e.message;
    return null;
  }
}

// Escapes explicitly rather than round-tripping through textContent ->
// innerHTML. That round-trip looks safe but uses the HTML spec's *text
// node* serialization, which replaces only & < > and U+00A0 -- quotes
// pass through untouched, since quote escaping only happens in attribute
// serialization mode. Every caller here interpolates into an attribute
// (alt=, title=), where a surviving " closes the attribute early and
// everything after it parses as new attributes -- turning an
// attacker-chosen Telegram filename into an onload= handler with access
// to window.pywebview.api (and therefore the pairing token).
function escapeHTML(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Right-click context menu. items: [{ label, action, destructive? }]
function showContextMenu(x, y, items) {
  document.querySelectorAll('.context-menu').forEach((m) => m.remove());
  const menu = document.createElement('div');
  menu.className = 'context-menu glass-material';
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  items.forEach((item) => {
    const el = document.createElement('div');
    el.className = `context-menu-item${item.destructive ? ' destructive' : ''}`;
    el.textContent = item.label;
    el.addEventListener('click', () => {
      menu.remove();
      item.action();
    });
    menu.appendChild(el);
  });
  document.body.appendChild(menu);
  const dismiss = (e) => {
    if (!menu.contains(e.target)) {
      menu.remove();
      document.removeEventListener('click', dismiss);
    }
  };
  setTimeout(() => document.addEventListener('click', dismiss), 0);
}

// Text-input modal, replacing window.prompt() -- pywebview's WKWebView
// backend implements the alert/confirm JS-dialog delegates but not the
// text-input one, so prompt() just returns null immediately with no UI.
// Resolves to the trimmed string, or null if cancelled/left empty.
function showPromptModal(title, placeholder = '') {
  return new Promise((resolve) => {
    const overlay = document.getElementById('prompt-modal-overlay');
    const titleEl = document.getElementById('prompt-modal-title');
    const input = document.getElementById('prompt-modal-input');
    const confirmBtn = document.getElementById('prompt-modal-confirm');
    const cancelBtn = document.getElementById('prompt-modal-cancel');

    titleEl.textContent = title;
    input.value = '';
    input.placeholder = placeholder;
    overlay.classList.remove('hidden');
    setTimeout(() => input.focus(), 0);

    function cleanup(value) {
      overlay.classList.add('hidden');
      confirmBtn.removeEventListener('click', onConfirm);
      cancelBtn.removeEventListener('click', onCancel);
      input.removeEventListener('keydown', onKeydown);
      resolve(value);
    }
    function onConfirm() { cleanup(input.value.trim() || null); }
    function onCancel() { cleanup(null); }
    function onKeydown(e) {
      if (e.key === 'Enter') onConfirm();
      if (e.key === 'Escape') onCancel();
    }
    confirmBtn.addEventListener('click', onConfirm);
    cancelBtn.addEventListener('click', onCancel);
    input.addEventListener('keydown', onKeydown);
  });
}

function uploadRowHTML(jobId, filename) {
  return `
    <div class="upload-row" id="upload-row-${jobId}">
      <button class="row-close-btn hidden" id="upload-dismiss-${jobId}" title="Dismiss" aria-label="Dismiss">&times;</button>
      <div class="upload-row-top">
        <span>${escapeHTML(filename)}</span>
        <span class="upload-row-status" id="upload-status-${jobId}">Starting&hellip;</span>
      </div>
      <div class="progress-bar"><div class="progress-bar-fill" id="upload-fill-${jobId}" style="width:0%"></div></div>
    </div>`;
}

// A failed upload row stays put (unlike a successful one, which
// auto-clears) until the user dismisses it -- large batches can leave
// several of these behind and nobody wants them piling up forever with no
// way to clear them.
function showUploadDismissBtn(id) {
  const btn = document.getElementById(`upload-dismiss-${id}`);
  if (!btn) return;
  btn.classList.remove('hidden');
  btn.addEventListener('click', () => document.getElementById(`upload-row-${id}`)?.remove());
}

// Generic job-progress poller: resolves with the final job on "done",
// rejects with an Error on "error" or a request failure. onTick is called
// with each intermediate job status. Shared by upload progress (below) and
// download/prepare progress (gallery.js).
function pollJobProgress(url, onTick) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      let job;
      try {
        job = await API.request(url);
      } catch (e) {
        reject(e);
        return;
      }
      if (onTick) onTick(job);
      if (job.status === 'error') {
        reject(new Error(job.error || 'Failed'));
        return;
      }
      if (job.status === 'done') {
        resolve(job);
        return;
      }
      setTimeout(tick, 700);
    };
    tick();
  });
}

// onSettled fires exactly once per file, on success OR failure -- required
// so a batch queue (queueBatchUpload) can advance past a failed file
// instead of stalling forever waiting for a slot that never frees up.
function pollUploadProgress(jobId, onSettled) {
  const statusEl = document.getElementById(`upload-status-${jobId}`);
  const fillEl = document.getElementById(`upload-fill-${jobId}`);
  pollJobProgress(`/api/upload/${jobId}/progress`, (job) => {
    if (fillEl) fillEl.style.width = `${job.percent || 0}%`;
    if (statusEl) statusEl.textContent = job.status;
  })
    .then(() => {
      if (statusEl) statusEl.textContent = 'Done';
      setTimeout(() => document.getElementById(`upload-row-${jobId}`)?.remove(), 3000);
    })
    .catch((e) => {
      if (statusEl) { statusEl.textContent = e.message; statusEl.classList.add('error'); }
      showUploadDismissBtn(jobId);
    })
    .finally(() => { if (onSettled) onSettled(); });
}

function handleUpload(file, albumId, uploadsContainerEl, onSettled) {
  const rowId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  uploadsContainerEl.insertAdjacentHTML('afterbegin', uploadRowHTML(rowId, file.name));
  API.upload('/api/upload', file, { album_id: albumId })
    .then(({ job_id }) => {
      document.getElementById(`upload-row-${rowId}`).id = `upload-row-${job_id}`;
      document.getElementById(`upload-status-${rowId}`).id = `upload-status-${job_id}`;
      document.getElementById(`upload-fill-${rowId}`).id = `upload-fill-${job_id}`;
      document.getElementById(`upload-dismiss-${rowId}`).id = `upload-dismiss-${job_id}`;
      pollUploadProgress(job_id, onSettled);
    })
    .catch((e) => {
      const statusEl = document.getElementById(`upload-status-${rowId}`);
      if (statusEl) { statusEl.textContent = e.message; statusEl.classList.add('error'); }
      showUploadDismissBtn(rowId);
      if (onSettled) onSettled();
    });
}

const LARGE_BATCH_FILE_COUNT = 10;
const LARGE_BATCH_BYTES = 2 * 1024 * 1024 * 1024; // 2GB

// Files are staged in fixed-size batches rather than fired all at once:
// batch N+1 doesn't start until every file in batch N has settled, so at
// most UPLOAD_BATCH_SIZE files ever sit as temp copies on the server's disk
// at the same time -- this is what actually caps the temp-disk spike risk
// (the backend's upload semaphore only throttles concurrent Telegram
// sends, not how many files get accepted/buffered up front). It also gives
// very large uploads a natural "Batch X of Y" checkpoint to show progress
// against, rather than one flat, meaningless "N/4000" counter.
const UPLOAD_BATCH_SIZE = 25;
const LARGE_UPLOAD_BATCH_THRESHOLD = 300;

// Entry point for every upload trigger (dropzones, grid drag-drop, native
// file pickers) -- warns before a large batch, pre-checks disk space, then
// hands off to the batched queue.
// Estimates peak temp-disk usage as one batch's worth (since batches are
// processed one at a time -- see UPLOAD_BATCH_SIZE) and warns if free space
// is tight. Returns true to proceed (space is fine, or the user chose to
// continue anyway), false to abort.
async function checkDiskSpaceOrConfirm(files) {
  const batchBytes = files
    .slice(0, UPLOAD_BATCH_SIZE)
    .reduce((sum, f) => sum + f.size, 0);
  let freeBytes;
  try {
    freeBytes = await window.pywebview.api.get_free_disk_space_bytes();
  } catch (e) {
    return true; // can't check -- don't block the upload over it
  }
  const neededBytes = batchBytes * 2; // safety margin
  if (freeBytes >= neededBytes) return true;
  return confirm(
    `Low disk space: only ${formatBytes(freeBytes)} free, and this batch may need up to `
    + `${formatBytes(batchBytes)} temporarily. Continue anyway?`,
  );
}

async function queueBatchUpload(fileListOrArray, albumId, uploadsContainerEl, onEachDone) {
  const files = Array.from(fileListOrArray);
  if (!files.length) return;

  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);
  if (files.length > LARGE_BATCH_FILE_COUNT || totalBytes > LARGE_BATCH_BYTES) {
    let message = `Upload ${files.length} files (${formatBytes(totalBytes)})? Large batches are queued a `
      + `couple at a time to avoid Telegram rate limits, so this may take a while.`;
    if (files.length > LARGE_UPLOAD_BATCH_THRESHOLD) {
      const batchCount = Math.ceil(files.length / UPLOAD_BATCH_SIZE);
      message += ` This will run as ${batchCount} batches of up to ${UPLOAD_BATCH_SIZE} files each.`;
    }
    if (!confirm(message)) return;

    if (!(await checkDiskSpaceOrConfirm(files))) return;
  }

  // Awaited (not fire-and-forget): callers that need to know when the
  // whole batch has genuinely finished -- e.g. before resetting a file
  // input's value, see album-gallery.js -- rely on this promise not
  // resolving early. WebKit has a long-documented bug where clearing a
  // file input's .value can invalidate the data of File objects obtained
  // from it that haven't been read yet, even ones already referenced
  // elsewhere in JS; for a 750-file batch spread over many sequential
  // server-side batches, resetting the input seconds after selection
  // (instead of after the whole thing finishes) meant nearly every file's
  // data was gone by the time its turn came up, surfacing as the backend
  // reporting the "file" field as missing on almost every upload.
  await runUploadQueue(files, albumId, uploadsContainerEl, onEachDone);
}

// Pause only stops the *next* batch from starting -- the batch already in
// flight (up to UPLOAD_BATCH_SIZE files) keeps going, since aborting
// in-flight network requests cleanly is a lot messier than just not
// starting new ones. That means Pause takes effect within a batch or two,
// not instantly.
function waitWhilePaused(pauseState) {
  return new Promise((resolve) => {
    (function check() {
      if (!pauseState.paused) { resolve(); return; }
      setTimeout(check, 300);
    })();
  });
}

async function runUploadQueue(files, albumId, uploadsContainerEl, onEachDone) {
  const total = files.length;
  const batchCount = Math.ceil(total / UPLOAD_BATCH_SIZE);
  const showBatches = total > LARGE_UPLOAD_BATCH_THRESHOLD;
  let settled = 0;
  const pauseState = { paused: false };

  let summaryEl = null;
  let summaryTextEl = null;
  let pauseBtnEl = null;
  if (total > 1) {
    const summaryId = `upload-batch-summary-${Date.now()}`;
    uploadsContainerEl.insertAdjacentHTML('afterbegin', `
      <div class="upload-batch-summary" id="${summaryId}">
        <span class="upload-batch-summary-text"></span>
        <button class="btn btn-secondary upload-pause-btn">Pause</button>
      </div>`);
    summaryEl = document.getElementById(summaryId);
    summaryTextEl = summaryEl.querySelector('.upload-batch-summary-text');
    pauseBtnEl = summaryEl.querySelector('.upload-pause-btn');
    pauseBtnEl.addEventListener('click', () => {
      pauseState.paused = !pauseState.paused;
      pauseBtnEl.textContent = pauseState.paused ? 'Resume' : 'Pause';
      updateSummary(0);
    });
  }

  function updateSummary(batchIndex) {
    if (!summaryTextEl) return;
    if (settled >= total) {
      summaryTextEl.textContent = `Uploaded ${total}/${total}`;
      if (pauseBtnEl) pauseBtnEl.classList.add('hidden');
      return;
    }
    const pausedNote = pauseState.paused ? ' (paused — finishing current batch)' : '';
    summaryTextEl.textContent = (showBatches
      ? `Batch ${batchIndex + 1} of ${batchCount} — ${settled}/${total} uploaded…`
      : `Uploaded ${settled}/${total}…`) + pausedNote;
  }

  for (let batchIndex = 0; batchIndex < batchCount; batchIndex++) {
    await waitWhilePaused(pauseState);
    const batchFiles = files.slice(batchIndex * UPLOAD_BATCH_SIZE, (batchIndex + 1) * UPLOAD_BATCH_SIZE);
    await Promise.all(batchFiles.map((file) => new Promise((resolve) => {
      handleUpload(file, albumId, uploadsContainerEl, () => {
        settled++;
        updateSummary(batchIndex);
        onEachDone();
        resolve();
      });
    })));
  }

  if (summaryEl) setTimeout(() => summaryEl.remove(), 4000);
}

// Some drag sources (notably Photos.app, and some other "promise drag"
// providers) don't hand WKWebView real file bytes -- the browser gets a
// placeholder File with no content, which then fails multipart upload with
// a confusing "field required" error from the backend. Filtering these out
// up front turns that into a clear, actionable message instead. Plain
// Finder drags, and the native file picker (used by the "+"/"choose
// files" buttons), aren't affected by this -- only OS-level drag payloads.
function filterValidDroppedFiles(fileList) {
  const files = Array.from(fileList);
  const valid = files.filter((f) => f && f.size > 0);
  const skipped = files.length - valid.length;
  // Always report skipped files, not just when the whole drop was invalid --
  // in a large batch a handful of unreadable files used to vanish with no
  // trace, leaving the total silently short with no way to tell what
  // happened to them.
  if (skipped > 0 && valid.length === 0) {
    alert("Couldn't read the dropped file(s) — this can happen when dragging directly from Photos.app. Try dragging from Finder instead, or use the picker button.");
  } else if (skipped > 0) {
    const names = files.filter((f) => !(f && f.size > 0)).map((f) => f.name).slice(0, 10).join('\n');
    const more = skipped > 10 ? `\n…and ${skipped - 10} more` : '';
    alert(
      `${skipped} of ${files.length} file(s) couldn't be read and ${skipped === 1 ? 'was' : 'were'} skipped `
      + `(this can happen when dragging directly from Photos.app, or a file that's mid-sync/mid-download):\n\n${names}${more}\n\n`
      + `Everything else was queued normally.`
    );
  }
  return valid;
}

// Copies text to the clipboard and briefly flashes the triggering button's
// label to confirm it happened, since there's no other feedback for a
// clipboard write.
async function copyToClipboard(text, btnEl) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    alert("Couldn't copy to clipboard.");
    return;
  }
  if (!btnEl) return;
  const original = btnEl.textContent;
  btnEl.textContent = 'Copied!';
  setTimeout(() => { btnEl.textContent = original; }, 1500);
}

function initSettingsView() {
  const watchFolderLabel = document.getElementById('current-watch-folder');
  const watchFolderInput = document.getElementById('input-watch-folder');
  const saveBtn = document.getElementById('btn-save-watch-folder');
  const photosBackupStatus = document.getElementById('photos-backup-status');
  const photosPermissionStatus = document.getElementById('photos-permission-status');
  const openAutomationSettingsBtn = document.getElementById('btn-open-automation-settings');
  const pollIntervalInput = document.getElementById('input-poll-interval');
  const savePollIntervalBtn = document.getElementById('btn-save-poll-interval');
  const photosLastChecked = document.getElementById('photos-last-checked');
  const photosBackedUpCount = document.getElementById('photos-backed-up-count');
  const deleteAfterDaysInput = document.getElementById('input-delete-after-days');
  const saveDeleteAfterDaysBtn = document.getElementById('btn-save-delete-after-days');
  const photosPendingCount = document.getElementById('photos-pending-count');
  const photosDeleteNowBtn = document.getElementById('btn-photos-delete-now');
  const photosDeleteNowResult = document.getElementById('photos-delete-now-result');
  const photosBackupToggle = document.getElementById('toggle-photos-backup');
  const viewPhotosAlbumLink = document.getElementById('link-view-photos-backup-album');
  let photosBackupAlbumId = null;
  const diskUsageTotal = document.getElementById('disk-usage-total');
  const diskUsageBreakdown = document.getElementById('disk-usage-breakdown');
  const refreshDiskUsageBtn = document.getElementById('btn-refresh-disk-usage');
  const cacheMaxInput = document.getElementById('input-cache-max-gb');
  const saveCacheMaxBtn = document.getElementById('btn-save-cache-max');

  API.request('/api/settings').then((settings) => {
    watchFolderLabel.textContent = settings.watch_folder || 'Not set';
    watchFolderInput.value = settings.watch_folder || '';
  });

  async function refreshDiskUsage() {
    diskUsageTotal.textContent = 'Calculating…';
    try {
      const usage = await API.request('/api/settings/disk_usage');
      diskUsageTotal.textContent = `${formatBytes(usage.total_bytes)} of ${formatBytes(usage.max_bytes)} cache limit`;
      diskUsageBreakdown.textContent =
        `Cached originals: ${formatBytes(usage.media_cache_bytes)} · `
        + `Thumbnails: ${formatBytes(usage.thumbnails_bytes)} · `
        + `Index: ${formatBytes(usage.database_bytes)}`;
      cacheMaxInput.value = (usage.max_bytes / 1024 ** 3).toFixed(1);
    } catch (e) {
      diskUsageTotal.textContent = e.message;
    }
  }
  refreshDiskUsage();
  refreshDiskUsageBtn.addEventListener('click', refreshDiskUsage);

  saveCacheMaxBtn.addEventListener('click', async () => {
    const gigabytes = parseFloat(cacheMaxInput.value);
    try {
      await API.request('/api/settings/media_cache_max_bytes', { method: 'PUT', json: { gigabytes } });
      await refreshDiskUsage();
    } catch (e) {
      alert(e.message);
    }
  });

  async function refreshPhotosBackupSettings() {
    const settings = await API.request('/api/photos_backup/settings');
    pollIntervalInput.value = settings.poll_interval_minutes;
    photosLastChecked.textContent = settings.last_checked_at
      ? new Date(settings.last_checked_at * 1000).toLocaleString() : 'never';
    photosBackedUpCount.textContent = String(settings.backed_up_count);
    deleteAfterDaysInput.value = settings.delete_after_days;
    photosPendingCount.textContent = String(settings.pending_deletion_count);
    photosDeleteNowBtn.disabled = settings.pending_deletion_count === 0;
    photosBackupToggle.classList.toggle('on', !!settings.enabled);
    photosBackupAlbumId = settings.album_id;
    // No album exists until the first item is ever backed up (see
    // routes_photos_backup.get_photos_backup_settings) -- nothing to link to yet.
    viewPhotosAlbumLink.classList.toggle('hidden', !photosBackupAlbumId);
    return settings;
  }

  // Live "is a poll cycle actively running right now" indicator -- polled
  // continuously (not just while Settings happens to be open) so the count
  // refreshes itself the moment a cycle finishes, without the user needing
  // to reload anything.
  let wasBackingUp = false;
  async function pollBackupStatus() {
    try {
      const status = await API.request('/api/photos_backup/status');
      photosBackupStatus.textContent = status.active
        ? `Backing up now… (${status.done} of ${status.total})`
        : 'Idle';
      if (wasBackingUp && !status.active) {
        await refreshPhotosBackupSettings();
      }
      wasBackingUp = status.active;
    } catch (e) {
      // Non-fatal -- leave the last known status text showing.
    }
  }
  pollBackupStatus();
  setInterval(pollBackupStatus, 3000);

  function updatePermissionStatusUI(status) {
    const labels = {
      granted: 'Granted',
      denied: 'Not granted — previously denied',
      undetermined: 'Waiting for you to approve the system permission prompt…',
      error: 'Could not check permission status',
    };
    photosPermissionStatus.textContent = labels[status] || status;
    openAutomationSettingsBtn.classList.toggle('hidden', status === 'granted');
  }

  let permissionPollCount = 0;
  async function pollPermissionStatus() {
    try {
      const { status } = await API.request('/api/photos_backup/permission_status');
      updatePermissionStatusUI(status);
      // Keep checking while the system prompt is still up (or freshly
      // dismissed) so the status updates live without the user having to
      // reopen Settings -- capped so this doesn't poll forever.
      if (status === 'undetermined' && permissionPollCount < 40) {
        permissionPollCount += 1;
        setTimeout(pollPermissionStatus, 3000);
      }
    } catch (e) {
      photosPermissionStatus.textContent = e.message;
    }
  }

  refreshPhotosBackupSettings().then((settings) => {
    // Only probe permission status automatically if the feature is already
    // enabled -- merely opening Settings shouldn't trigger the system
    // Automation prompt for a feature nobody turned on yet.
    if (settings.enabled) {
      pollPermissionStatus();
    } else {
      photosPermissionStatus.textContent = 'Not checked yet';
      openAutomationSettingsBtn.classList.add('hidden');
    }
  });
  openAutomationSettingsBtn.addEventListener('click', () => {
    window.pywebview.api.open_automation_settings();
  });

  savePollIntervalBtn.addEventListener('click', async () => {
    const minutes = parseInt(pollIntervalInput.value, 10);
    try {
      await API.request('/api/photos_backup/poll_interval', { method: 'PUT', json: { minutes } });
    } catch (e) {
      alert(e.message);
    }
  });

  // Backed-up photos are browsable with real thumbnails in their own
  // auto-created album -- rather than a second, worse (text-only) view of
  // the same rows here in Settings, this just jumps straight to it.
  viewPhotosAlbumLink.addEventListener('click', async (e) => {
    e.preventDefault();
    if (!photosBackupAlbumId) return;
    try {
      const albums = await API.request('/api/albums');
      const album = albums.find((a) => a.id === photosBackupAlbumId);
      if (!album) return;
      document.querySelector('.nav-item[data-view="albums"]').click();
      openAlbum(album);
    } catch (err) {
      alert(err.message);
    }
  });

  saveDeleteAfterDaysBtn.addEventListener('click', async () => {
    const days = parseInt(deleteAfterDaysInput.value, 10);
    try {
      await API.request('/api/photos_backup/delete_after_days', { method: 'PUT', json: { days } });
      await refreshPhotosBackupSettings();
    } catch (e) {
      alert(e.message);
    }
  });

  photosDeleteNowBtn.addEventListener('click', async () => {
    const pending = photosPendingCount.textContent;
    if (!confirm(
      `Remove ${pending} backed-up photo(s) from your Photos library now?\n\n`
      + 'They are already in Telegram, and will go to Recently Deleted (Apple\u2019s 30-day undo '
      + 'window) first. Because iCloud Photos syncs, this also removes them from your iPhone.\n\n'
      + 'macOS will ask you to confirm as well.'
    )) return;
    photosDeleteNowBtn.disabled = true;
    photosDeleteNowResult.textContent = 'Waiting for macOS to confirm\u2026';
    try {
      const { deleted } = await API.request('/api/photos_backup/delete_now', { method: 'POST' });
      photosDeleteNowResult.textContent = deleted
        ? `Removed ${deleted} photo(s). They're in Recently Deleted if you need them back.`
        : 'Nothing was removed.';
      await refreshPhotosBackupSettings();
    } catch (e) {
      photosDeleteNowResult.textContent = e.message;
    } finally {
      photosDeleteNowBtn.disabled = false;
    }
  });

  photosBackupToggle.addEventListener('click', async () => {
    const enabling = !photosBackupToggle.classList.contains('on');
    if (enabling) {
      const confirmed = confirm(
        'Automatic Photos Backup will upload every new photo to Telegram. Photos are only '
        + 'removed from your library once they pass the age you set below, and removal goes to '
        + 'Recently Deleted (Apple’s 30-day undo window) first. Because iCloud Photos syncs, '
        + 'anything removed here also disappears from your iPhone. Are you sure?'
      );
      if (!confirmed) return;
    }
    try {
      await API.request('/api/photos_backup/enable', { method: 'PUT', json: { enabled: enabling } });
      photosBackupToggle.classList.toggle('on', enabling);
      if (enabling) {
        permissionPollCount = 0;
        pollPermissionStatus();
      }
    } catch (e) {
      alert(e.message);
    }
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

  const syncBtn = document.getElementById('btn-sync');
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    await runSync({});
    syncBtn.disabled = false;
  });

  document.getElementById('btn-help-open-telegram-api').addEventListener('click', () => {
    window.pywebview.api.open_telegram_api_page();
  });

  const logoutBtn = document.getElementById('btn-logout');
  logoutBtn.addEventListener('click', async () => {
    if (!confirm(
      'Log out of Telegram? Your library stays indexed on this Mac and nothing in Telegram is '
      + 'touched -- signing back in with the same number picks up where you left off.'
    )) return;
    logoutBtn.disabled = true;
    try {
      await API.request('/api/setup/logout', { method: 'POST' });
      // Full reload rather than re-running checkSetupStatus() in place --
      // every init*View() function attaches its own event listeners, and
      // calling them a second time in the same page would double them up.
      window.location.reload();
    } catch (e) {
      alert(e.message);
      logoutBtn.disabled = false;
    }
  });
}

// --- Shared lightbox chrome (one overlay in the DOM, used by every gallery
// module) -- wired once via initLightboxChrome(), called from boot() below.

function closeLightbox() {
  // Cancels a still-in-flight prepare (e.g. a video not yet fully
  // downloaded) instead of letting it keep pulling the full file in the
  // background just because the lightbox closed.
  if (LIGHTBOX_OWNER && LIGHTBOX_OWNER.cancelActivePrepare) LIGHTBOX_OWNER.cancelActivePrepare();
  document.getElementById('lightbox-overlay').classList.add('hidden');
  document.getElementById('lightbox-media').innerHTML = '';
  LIGHTBOX_OWNER = null;
}

// Canonical shared entry points -- dispatch to whichever module currently
// owns the open lightbox. These are the only lightbox-nav names any other
// script should reference by name.
function lightboxPrev() {
  if (LIGHTBOX_OWNER) LIGHTBOX_OWNER.lightboxPrev();
}

function lightboxNext() {
  if (LIGHTBOX_OWNER) LIGHTBOX_OWNER.lightboxNext();
}

function initLightboxChrome() {
  document.getElementById('btn-lightbox-close').addEventListener('click', closeLightbox);
  document.getElementById('btn-lightbox-prev').addEventListener('click', lightboxPrev);
  document.getElementById('btn-lightbox-next').addEventListener('click', lightboxNext);
  document.getElementById('lightbox-overlay').addEventListener('click', (e) => {
    if (e.target.id === 'lightbox-overlay') closeLightbox();
  });
  document.addEventListener('keydown', (e) => {
    if (document.getElementById('lightbox-overlay').classList.contains('hidden')) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft') lightboxPrev();
    if (e.key === 'ArrowRight') lightboxNext();
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
    initLightboxChrome();
    initAlbumsView();
    initSettingsView();
    initTour();
    runSync({ toast: true }); // not awaited -- non-blocking, per the request
    API.request('/api/settings').then((settings) => {
      if (!settings.onboarding_completed) openTour();
    });
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

// Safety net: WKWebView's default action for a drop it doesn't otherwise
// handle is to navigate the whole window to the dropped file, replacing
// the app with a raw native image/file preview that has no way back (no
// close button, since it's not our UI). Every real drop target calls
// preventDefault() itself (album-gallery.js's grid), but this catches
// anything that misses -- a drop landing just outside a
// target, or racing with its handler.
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());
