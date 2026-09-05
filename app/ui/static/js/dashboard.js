/**
 * VEditor Dashboard — Live Status Polling + Instant Filter Logic
 */

const ACTIVE_STATUSES = new Set([
  'detecting', 'cutting', 'normalizing', 'rendering', 'transcoding', 'publishing'
]);

const STATUS_BADGE_MAP = {
  waiting_for_files: '<span class="badge badge-gray"><span class="badge-dot"></span>Waiting</span>',
  detecting:         '<span class="badge badge-amber badge-pulse"><span class="badge-dot"></span>Detecting</span>',
  approval_pending:  '<span class="badge badge-orange badge-pulse"><span class="badge-dot"></span>Pending Review</span>',
  cutting:           '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Cutting</span>',
  normalizing:       '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Normalizing</span>',
  rendering:         '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Rendering</span>',
  transcoding:       '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Transcoding</span>',
  preview:           '<span class="badge badge-teal"><span class="badge-dot"></span>Preview Ready</span>',
  publishing:        '<span class="badge badge-purple badge-pulse"><span class="badge-dot"></span>Publishing</span>',
  done:              '<span class="badge badge-green"><span class="badge-dot"></span>Done</span>',
  failed:            '<span class="badge badge-red"><span class="badge-dot"></span>Failed</span>',
};

function getActiveTalkIds() {
  return [...document.querySelectorAll('tr[data-talk-id]')]
    .filter(row => ACTIVE_STATUSES.has(row.dataset.status))
    .map(row => parseInt(row.dataset.talkId, 10));
}

async function pollTalk(talkId) {
  try {
    const key = window.getApiKey();
    if (!key) return;
    const r = await fetch(`/talks/${talkId}`, {
      headers: { 'X-API-Key': key }
    });
    if (!r.ok) return;
    const data = await r.json();
    const cell = document.querySelector(`.status-cell[data-talk-id="${talkId}"]`);
    const row  = document.querySelector(`tr[data-talk-id="${talkId}"]`);
    if (!cell) return;
    const newBadge = STATUS_BADGE_MAP[data.status] ?? STATUS_BADGE_MAP.waiting_for_files;
    cell.innerHTML = newBadge;
    if (row) row.dataset.status = data.status;
    if (data.status === 'done' || data.status === 'failed') {
      setTimeout(() => location.reload(), 1500);
    }
  } catch { /* skip */ }
}

function startPolling() {
  const ids = getActiveTalkIds();
  if (ids.length === 0) return;
  ids.forEach(id => pollTalk(id));
  setInterval(() => {
    getActiveTalkIds().forEach(id => pollTalk(id));
  }, 5000);
}

// ── Instant Live Filter on Typing ───────────────────────────────
const searchInput = document.getElementById('search-input');
const statusSelect = document.getElementById('status-select');
const rows = document.querySelectorAll('tbody tr[data-talk-id]');

function applyFilters() {
  const q = (searchInput ? searchInput.value : '').toLowerCase().trim();
  const selectedStatus = statusSelect ? statusSelect.value : '';

  let visibleCount = 0;
  rows.forEach(row => {
    const titleText = (row.querySelector('.td-title') ? row.querySelector('.td-title').textContent : '').toLowerCase();
    const rowStatus = row.dataset.status || '';

    const matchesQuery = !q || titleText.includes(q);
    const matchesStatus = !selectedStatus || rowStatus === selectedStatus;

    if (matchesQuery && matchesStatus) {
      row.style.display = '';
      visibleCount++;
    } else {
      row.style.display = 'none';
    }
  });

  const countEl = document.querySelector('.table-count');
  if (countEl) {
    countEl.textContent = `${visibleCount} result${visibleCount !== 1 ? 's' : ''}`;
  }

  const noMatchesRow = document.getElementById('no-client-matches-row');
  if (noMatchesRow) {
    noMatchesRow.style.display = visibleCount === 0 ? '' : 'none';
  }
}

if (searchInput) {
  searchInput.addEventListener('input', applyFilters);
}

if (statusSelect) {
  statusSelect.addEventListener('change', applyFilters);
}

// Keyboard accessibility
rows.forEach(row => {
  row.setAttribute('tabindex', '0');
  row.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      window.location = `/studio/talks/${row.dataset.talkId}`;
    }
  });
});

document.addEventListener('DOMContentLoaded', startPolling);

// ── Schedule Import & Quick Talk & Room Attach Modals ──────────
window.openImportModal = function() {
  const m = document.getElementById('modal-import');
  if (m) m.style.display = 'flex';
};

window.closeImportModal = function() {
  const m = document.getElementById('modal-import');
  if (m) m.style.display = 'none';
};

window.openAttachRoomModal = function() {
  const m = document.getElementById('modal-attach-room');
  if (m) m.style.display = 'flex';
};

window.closeAttachRoomModal = function() {
  const m = document.getElementById('modal-attach-room');
  if (m) m.style.display = 'none';
};

window.submitAttachRoomRecording = async function() {
  const roomInput = (document.getElementById('attach-room-input') || {}).value || '';
  const fileInput = document.getElementById('attach-room-file');
  const btn = document.getElementById('btn-submit-attach-room');
  const orig = btn ? btn.innerHTML : '';

  if (!roomInput.trim()) {
    alert('Please enter or select a room name.');
    return;
  }
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    alert('Please select a video recording file.');
    return;
  }

  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Attaching to Room Talks...'; }

  try {
    const fd = new FormData();
    fd.append('room', roomInput.trim());
    fd.append('file', fileInput.files[0]);

    const urlParams = new URLSearchParams(window.location.search);
    const eventIdParam = urlParams.get('event_id');
    if (eventIdParam) {
      fd.append('event_id', eventIdParam);
    }

    const res = await (window.authFetch || fetch)('/studio/room/attach-recording', {
      method: 'POST',
      body: fd,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    alert(`Successfully attached video to ${data.attached_count} session(s) in "${data.room}"!`);
    location.reload();
  } catch (err) {
    alert(`Attachment failed: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
};

window.openQuickTalkModal = function() {
  const m = document.getElementById('modal-quick-talk');
  if (m) m.style.display = 'flex';
};

window.closeQuickTalkModal = function() {
  const m = document.getElementById('modal-quick-talk');
  if (m) m.style.display = 'none';
};

window.submitScheduleImport = async function() {
  const fileInput = document.getElementById('import-file-input');
  const jsonText = (document.getElementById('import-json-textarea') || {}).value || '';
  const btn = document.getElementById('btn-submit-import');
  const orig = btn ? btn.innerHTML : '';

  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Importing...'; }

  try {
    let res;
    if (fileInput && fileInput.files && fileInput.files[0]) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      res = await (window.authFetch || fetch)('/studio/schedule/import', { method: 'POST', body: fd });
    } else if (jsonText.trim()) {
      let parsed;
      try { parsed = JSON.parse(jsonText); } catch { throw new Error('Invalid JSON format'); }
      res = await (window.authFetch || fetch)('/studio/schedule/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      });
    } else {
      throw new Error('Please select a JSON file or paste JSON content');
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    alert(`Successfully imported ${data.imported_count} session(s) into "${data.event_name}"!`);
    location.reload();
  } catch (err) {
    alert(`Import failed: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
};

window.submitQuickTalk = async function() {
  const eventName = (document.getElementById('quick-event-name') || {}).value || 'General Conference';
  const title = (document.getElementById('quick-talk-title') || {}).value || '';
  const room = (document.getElementById('quick-talk-room') || {}).value || 'Auditorium A';
  const duration = parseInt((document.getElementById('quick-talk-duration') || {}).value, 10) || 45;
  const startVal = (document.getElementById('quick-talk-start') || {}).value || '';
  const btn = document.getElementById('btn-submit-quick-talk');
  const orig = btn ? btn.innerHTML : '';

  if (!title.trim()) {
    alert('Please enter a talk title.');
    return;
  }

  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Creating...'; }

  try {
    const payload = {
      event_name: eventName,
      title,
      room,
      duration_minutes: duration,
    };
    if (startVal) {
      payload.start = new Date(startVal).toISOString();
    }

    const res = await (window.authFetch || fetch)('/studio/talks/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    window.location = `/studio/talks/${data.talk_id}`;
  } catch (err) {
    alert(`Failed to create talk: ${err.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
};

// ── Single & Bulk Delete Operations ─────────────────────────────
window.deleteSingleTalk = async function(id, title) {
  if (!confirm(`Are you sure you want to delete talk #${id}: "${title}"?\nThis will permanently delete all associated recording and media files.`)) {
    return;
  }

  try {
    const res = await (window.authFetch || fetch)(`/studio/talks/${id}/delete`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }
    location.reload();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
};

window.toggleSelectAllTalks = function(headerCheckbox) {
  const checkboxes = document.querySelectorAll('.talk-checkbox');
  checkboxes.forEach(cb => {
    const row = cb.closest('tr');
    if (row && row.style.display !== 'none') {
      cb.checked = headerCheckbox.checked;
    }
  });
  window.updateBulkSelectionUI();
};

window.updateBulkSelectionUI = function() {
  const selected = [...document.querySelectorAll('.talk-checkbox:checked')];
  const count = selected.length;
  const bar = document.getElementById('bulk-actions-bar');
  const countText = document.getElementById('selected-count-text');
  const deleteCount = document.getElementById('bulk-delete-count');
  const selectAll = document.getElementById('select-all-talks');

  if (bar) bar.style.display = count > 0 ? 'flex' : 'none';
  if (countText) countText.textContent = `${count} talk${count !== 1 ? 's' : ''} selected`;
  if (deleteCount) deleteCount.textContent = count;

  const totalVisible = [...document.querySelectorAll('.talk-checkbox')].filter(cb => {
    const r = cb.closest('tr');
    return r && r.style.display !== 'none';
  }).length;

  if (selectAll) {
    selectAll.checked = count > 0 && count === totalVisible;
    selectAll.indeterminate = count > 0 && count < totalVisible;
  }
};

window.clearBulkSelection = function() {
  document.querySelectorAll('.talk-checkbox').forEach(cb => { cb.checked = false; });
  const selectAll = document.getElementById('select-all-talks');
  if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
  window.updateBulkSelectionUI();
};

window.submitBulkDelete = async function() {
  const selected = [...document.querySelectorAll('.talk-checkbox:checked')].map(cb => parseInt(cb.value, 10));
  if (selected.length === 0) return;

  if (!confirm(`Are you sure you want to delete ${selected.length} selected talk(s)?\nThis will permanently delete all associated video and audio artifacts.`)) {
    return;
  }

  const btn = document.getElementById('btn-bulk-delete');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Deleting...'; }

  try {
    const res = await (window.authFetch || fetch)('/studio/talks/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ talk_ids: selected }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    location.reload();
  } catch (err) {
    alert(`Bulk delete failed: ${err.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
};

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.btn-delete-talk');
  if (!btn) return;
  e.stopPropagation();
  const tid = Number(btn.dataset.talkId);
  const title = btn.dataset.talkTitle || '';
  if (tid) window.deleteSingleTalk(tid, title);
});
