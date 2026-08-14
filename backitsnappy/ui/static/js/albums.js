// Albums view: Finder-style navigation only -- top level shows albums as
// folder icons in a grid with a breadcrumb; double-clicking drills into an
// album, handing off to album-gallery.js's independent Photos.app-style
// grid for that album's contents (this file never touches its DOM/state).

let ALBUMS = [];
let CURRENT_ALBUM = null; // null = top-level folder grid

// The fixed iPhone-backup album (matched by name, not a dedicated DB flag --
// this is a lightweight stand-in until the per-run album picker lands) gets
// a phone icon instead of a plain folder, so it reads at a glance as "this
// one's where Shortcuts uploads land" rather than a folder the user made.
function folderCellHTML(album) {
  const isPhoneBackup = album.name.trim().toLowerCase() === 'iphone backup';
  const icon = isPhoneBackup ? ICONS.iphone : ICONS.folder;
  return `
    <div class="folder-cell" data-album-id="${album.id}">
      <div class="folder-icon">${icon}</div>
      <div class="folder-name">${escapeHTML(album.name)}</div>
    </div>`;
}

function renderBreadcrumb() {
  const el = document.getElementById('albums-breadcrumb');
  if (!CURRENT_ALBUM) {
    el.innerHTML = `
      <span class="breadcrumb-item">BackitSnappy</span>
      <span class="breadcrumb-sep">&rsaquo;</span>
      <span class="breadcrumb-item active">Albums</span>`;
    return;
  }
  el.innerHTML = `
    <span class="breadcrumb-item">BackitSnappy</span>
    <span class="breadcrumb-sep">&rsaquo;</span>
    <span class="breadcrumb-item breadcrumb-link" id="breadcrumb-albums">Albums</span>
    <span class="breadcrumb-sep">&rsaquo;</span>
    <span class="breadcrumb-item active">${escapeHTML(CURRENT_ALBUM.name)}</span>`;
  document.getElementById('breadcrumb-albums').addEventListener('click', showAlbumsGrid);
}

// The toolbar's action button is contextual: "+ New Folder" at the top
// level, "+ Add Photos" once inside an album -- never both, and never
// New Folder while drilled in (it doesn't apply there).
function updateToolbarButtons() {
  document.getElementById('btn-new-folder').classList.toggle('hidden', !!CURRENT_ALBUM);
  document.getElementById('album-toolbar-actions').classList.toggle('hidden', !CURRENT_ALBUM);
}

function showAlbumsGrid() {
  CURRENT_ALBUM = null;
  document.getElementById('albums-grid-view').classList.remove('hidden');
  document.getElementById('album-drilldown-view').classList.add('hidden');
  renderBreadcrumb();
  updateToolbarButtons();
  refreshAlbumsGrid();
}

function openAlbum(album) {
  CURRENT_ALBUM = album;
  document.getElementById('albums-grid-view').classList.add('hidden');
  document.getElementById('album-drilldown-view').classList.remove('hidden');
  renderBreadcrumb();
  updateToolbarButtons();
  openAlbumGallery(album.id);
}

async function confirmDeleteAlbum(album) {
  if (!confirm(`Delete "${album.name}" and everything inside it? This can't be undone.`)) return;
  try {
    await API.request(`/api/albums/${album.id}`, { method: 'DELETE' });
    if (CURRENT_ALBUM && CURRENT_ALBUM.id === album.id) {
      showAlbumsGrid();
    } else {
      await refreshAlbumsGrid();
    }
  } catch (e) {
    alert(e.message);
  }
}

async function refreshAlbumsGrid() {
  ALBUMS = await API.request('/api/albums');
  const grid = document.getElementById('albums-folder-grid');
  grid.innerHTML = ALBUMS.length
    ? ALBUMS.map(folderCellHTML).join('')
    : '<div class="gallery-empty">No albums yet — create one with "+ New Folder."</div>';

  grid.querySelectorAll('.folder-cell').forEach((cell) => {
    const album = ALBUMS.find((a) => a.id === Number(cell.dataset.albumId));
    cell.addEventListener('dblclick', () => openAlbum(album));
    cell.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showContextMenu(e.pageX, e.pageY, [
        { label: 'Open', action: () => openAlbum(album) },
        { label: 'Delete Album', destructive: true, action: () => confirmDeleteAlbum(album) },
      ]);
    });
  });
}

function initAlbumsView() {
  document.getElementById('btn-new-folder').addEventListener('click', async () => {
    const name = await showPromptModal('New Album', 'Album name');
    if (!name) return;
    try {
      await API.request('/api/albums', { method: 'POST', json: { name } });
      await refreshAlbumsGrid();
    } catch (e) {
      alert(e.message);
    }
  });

  initAlbumGallery();

  renderBreadcrumb();
  updateToolbarButtons();
  refreshAlbumsGrid();
}
