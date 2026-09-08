/**
 * VEditor Dashboard — Live Status Polling + Instant Filter Logic
 */

const ACTIVE_STATUSES = new Set([
  'detecting', 'cutting', 'generating_previews', 'normalizing', 'rendering', 'transcoding', 'uploading', 'publishing'
]);

const STATUS_BADGE_MAP = {
  waiting_for_files:   '<span class="badge badge-gray"><span class="badge-dot"></span>Waiting</span>',
  detecting:           '<span class="badge badge-amber badge-pulse"><span class="badge-dot"></span>Detecting</span>',
  approval_pending:    '<span class="badge badge-orange badge-pulse"><span class="badge-dot"></span>Pending Review</span>',
  pending_approval:    '<span class="badge badge-orange badge-pulse"><span class="badge-dot"></span>Pending Review</span>',
  pending_bounds:      '<span class="badge badge-orange badge-pulse"><span class="badge-dot"></span>Pending Bounds</span>',
  cutting:             '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Cutting</span>',
  generating_previews: '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Generating Previews</span>',
  normalizing:         '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Normalizing</span>',
  rendering:           '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Rendering</span>',
  transcoding:         '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Transcoding</span>',
  uploading:           '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Uploading</span>',
  preview:             '<span class="badge badge-teal"><span class="badge-dot"></span>Preview Ready</span>',
  publishing:          '<span class="badge badge-purple badge-pulse"><span class="badge-dot"></span>Publishing</span>',
  done:                '<span class="badge badge-green"><span class="badge-dot"></span>Done</span>',
  rejected:            '<span class="badge badge-red"><span class="badge-dot"></span>Rejected</span>',
  failed:              '<span class="badge badge-red"><span class="badge-dot"></span>Failed</span>',
  broken:              '<span class="badge badge-red"><span class="badge-dot"></span>Broken</span>',
};

function getActiveTalkIds() {
  return [...document.querySelectorAll('tr[data-talk-id]')]
    .filter(row => ACTIVE_STATUSES.has(row.dataset.status))
    .map(row => parseInt(row.dataset.talkId, 10));
}

function renderActiveJobCell(cell, statusText, pct, remainingStr) {
  cell.textContent = '';

  const container = document.createElement('div');
  container.style.cssText = 'display:flex;flex-direction:column;gap:3px;min-width:110px;';

  const headerDiv = document.createElement('div');
  headerDiv.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:4px;';

  const statusBadge = document.createElement('span');
  statusBadge.className = 'badge badge-blue badge-pulse';
  const dot = document.createElement('span');
  dot.className = 'badge-dot';
  statusBadge.appendChild(dot);
  statusBadge.appendChild(document.createTextNode((statusText || 'processing').replace(/_/g, ' ')));

  const pctBadge = document.createElement('span');
  pctBadge.className = 'badge badge-info';
  pctBadge.style.cssText = 'font-size:0.65rem;padding:1px 4px;';
  pctBadge.textContent = `${pct}%`;

  headerDiv.appendChild(statusBadge);
  headerDiv.appendChild(pctBadge);

  const trackDiv = document.createElement('div');
  trackDiv.className = 'job-progress-track';
  trackDiv.style.cssText = 'height:3px;margin:0;';

  const fillDiv = document.createElement('div');
  fillDiv.className = 'job-progress-fill animated';
  fillDiv.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  trackDiv.appendChild(fillDiv);

  container.appendChild(headerDiv);
  container.appendChild(trackDiv);

  if (remainingStr) {
    const remSpan = document.createElement('span');
    remSpan.style.cssText = 'font-size:0.65rem;color:var(--v-text-muted);font-variant-numeric:tabular-nums;';
    remSpan.textContent = remainingStr;
    container.appendChild(remSpan);
  }

  cell.appendChild(container);
}

async function pollTalk(talkId) {
  try {
    const key = (window.getApiKey && window.getApiKey()) || '';
    const cell = document.querySelector(`.status-cell[data-talk-id="${talkId}"]`);
    const row  = document.querySelector(`tr[data-talk-id="${talkId}"]`);
    if (!cell) return;

    let talkStatus = row ? row.dataset.status : '';
    let jobs = [];

    if (key) {
      const headers = { 'X-API-Key': key };
      const r = await (window.authFetch || fetch)(`/talks/${talkId}`, { headers });
      if (!r.ok) return;
      const data = await r.json();
      talkStatus = data.status;
      jobs = data.jobs || [];
    } else {
      const r = await fetch(`/studio/talks/${talkId}/jobs`);
      if (!r.ok) return;
      const data = await r.json();
      talkStatus = data.status || talkStatus;
      jobs = data.jobs || (Array.isArray(data) ? data : []);
    }

    const activeJob = jobs.find(j => j.status === 'running');
    if (activeJob && activeJob.progress_pct !== null && activeJob.progress_pct !== undefined) {
      const pct = Math.round(activeJob.progress_pct);
      const remainingStr = (activeJob.estimated_remaining !== null && activeJob.estimated_remaining !== undefined)
        ? `~${Math.round(activeJob.estimated_remaining)}s left`
        : (activeJob.elapsed_time !== null && activeJob.elapsed_time !== undefined ? `${Math.round(activeJob.elapsed_time)}s elapsed` : '');

      renderActiveJobCell(cell, talkStatus || activeJob.kind, pct, remainingStr);
    } else if (talkStatus) {
      const newBadge = STATUS_BADGE_MAP[talkStatus] ?? STATUS_BADGE_MAP.waiting_for_files;
      cell.innerHTML = newBadge;
    }

    if (row && talkStatus) row.dataset.status = talkStatus;
    if (talkStatus === 'done' || talkStatus === 'failed') {
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
  }, 3000);
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
