// Albums view: create/list albums, upload into an album, invite members.

let SELECTED_ALBUM = null;

function albumRowHTML(album) {
  const date = new Date(album.created_at * 1000).toLocaleDateString();
  return `<div class="list-row" data-album-id="${album.id}" style="cursor:pointer;">
    <div style="flex:1;">
      <div class="list-row-title">${escapeHTML(album.name)}</div>
      <div class="list-row-subtitle">Created ${date}</div>
    </div>
  </div>`;
}

async function refreshAlbumList() {
  const albums = await API.request('/api/albums');
  const listEl = document.getElementById('album-list');
  listEl.innerHTML = albums.length
    ? albums.map(albumRowHTML).join('')
    : '<div class="list-row"><div class="list-row-subtitle">No albums yet</div></div>';
  listEl.querySelectorAll('.list-row[data-album-id]').forEach((row) => {
    row.addEventListener('click', () => selectAlbum(albums.find((a) => a.id === Number(row.dataset.albumId))));
  });
}

function selectAlbum(album) {
  SELECTED_ALBUM = album;
  document.getElementById('album-detail').classList.remove('hidden');
  document.getElementById('album-detail-title').textContent = album.name;
  document.getElementById('invite-result').innerHTML = '';
  const fileList = document.getElementById('file-list-album');
  refreshFileList(album.id, fileList);
}

function initAlbumsView() {
  const createBtn = document.getElementById('btn-create-album');
  const nameInput = document.getElementById('input-album-name');
  createBtn.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    if (!name) return;
    createBtn.disabled = true;
    try {
      await API.request('/api/albums', { method: 'POST', json: { name } });
      nameInput.value = '';
      await refreshAlbumList();
    } catch (e) {
      alert(e.message);
    } finally {
      createBtn.disabled = false;
    }
  });

  const dropzone = document.getElementById('dropzone-album');
  const browseBtn = document.getElementById('btn-browse-album');
  const fileInput = document.getElementById('file-input-album');
  const uploads = document.getElementById('uploads-album');
  wireDropzone(dropzone, browseBtn, fileInput, (files) => {
    if (!SELECTED_ALBUM) { alert('Select an album first'); return; }
    const fileList = document.getElementById('file-list-album');
    Array.from(files).forEach((f) => handleUpload(f, SELECTED_ALBUM.id, uploads, fileList));
  });

  const inviteBtn = document.getElementById('btn-invite');
  const inviteInput = document.getElementById('input-invite-username');
  inviteBtn.addEventListener('click', async () => {
    if (!SELECTED_ALBUM) { alert('Select an album first'); return; }
    const username = inviteInput.value.trim();
    if (!username) return;
    const resultEl = document.getElementById('invite-result');
    inviteBtn.disabled = true;
    resultEl.textContent = 'Inviting…';
    try {
      const result = await API.request(`/api/albums/${SELECTED_ALBUM.id}/invite`, {
        method: 'POST', json: { username },
      });
      if (result.method === 'direct') {
        resultEl.textContent = `Added @${username} directly.`;
      } else {
        resultEl.innerHTML = `@${username} couldn't be added directly (their privacy settings block it) — share this invite link instead:
          <div class="invite-link-box">${escapeHTML(result.invite_link)}</div>`;
      }
      inviteInput.value = '';
    } catch (e) {
      resultEl.textContent = e.message;
    } finally {
      inviteBtn.disabled = false;
    }
  });

  refreshAlbumList();
}
