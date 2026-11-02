/**
 * VEditor Dashboard — Live Status Polling + Instant Filter Logic
 */

const ACTIVE_STATUSES = new Set([
  'detecting', 'cutting', 'generating_previews', 'normalizing', 'assembling', 'rendering', 'transcoding', 'uploading', 'publishing'
]);

const STATUS_BADGE_MAP = {
  waiting_for_files:   '<span class="badge badge-waiting"><span class="badge-dot"></span>Waiting for files</span>',
  detecting:           '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Detecting</span>',
  approval_pending:    '<span class="badge badge-pending"><span class="badge-dot"></span>Pending Review</span>',
  pending_approval:    '<span class="badge badge-pending"><span class="badge-dot"></span>Pending Review</span>',
  pending_intro_outro: '<span class="badge badge-pending"><span class="badge-dot"></span>Pending Bumpers</span>',
  pending_bounds:      '<span class="badge badge-pending"><span class="badge-dot"></span>Pending Bounds</span>',
  needs_work:          '<span class="badge badge-pending"><span class="badge-dot"></span>Needs Work</span>',
  cutting:             '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Cutting</span>',
  generating_previews: '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Generating Previews</span>',
  normalizing:         '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Normalizing</span>',
  assembling:          '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Assembling</span>',
  rendering:           '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Rendering</span>',
  transcoding:         '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Transcoding</span>',
  uploading:           '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Uploading</span>',
  preview:             '<span class="badge badge-preview"><span class="badge-dot"></span>Preview Ready</span>',
  publishing:          '<span class="badge badge-processing"><span class="spinner spinner-sm"></span>Publishing</span>',
  done:                '<span class="badge badge-done"><span class="badge-dot"></span>Finished</span>',
  rejected:            '<span class="badge badge-danger"><span class="badge-dot"></span>Rejected</span>',
  failed:              '<span class="badge badge-danger"><span class="badge-dot"></span>Failed</span>',
  broken:              '<span class="badge badge-danger"><span class="badge-dot"></span>Broken</span>',
};

function getActiveTalkIds() {
  return [...document.querySelectorAll('tr[data-talk-id]')]
    .filter(row => ACTIVE_STATUSES.has(row.dataset.status))
    .map(row => parseInt(row.dataset.talkId, 10));
}

async function pollTalk(talkId) {
  try {
    const isSessionLoggedIn = Boolean(
      document.querySelector('.user-email') ||
      document.getElementById('logout-btn')
    );
    const key = (window.getApiKey && window.getApiKey()) || '';
    if (!key && !isSessionLoggedIn) return;
    const cell = document.querySelector(`.status-cell[data-talk-id="${talkId}"]`);
    const row  = document.querySelector(`tr[data-talk-id="${talkId}"]`);
    if (!cell) return;

    let talkStatus = row ? row.dataset.status : '';

    const headers = key ? { 'X-API-Key': key } : {};
    const r = await (window.authFetch || fetch)(`/talks/${talkId}/jobs`, { headers, _isPolling: true });
    if (!r.ok) return;
    const data = await r.json();
    talkStatus = data.status || talkStatus;

    if (talkStatus) {
      const newBadge = STATUS_BADGE_MAP[talkStatus] ?? `<span class="badge badge-waiting"><span class="badge-dot"></span>${talkStatus}</span>`;
      cell.innerHTML = newBadge;
    }

    if (row && talkStatus) row.dataset.status = talkStatus;
  } catch { /* skip */ }
}

function startPolling() {
  const isSessionLoggedIn = Boolean(
    document.querySelector('.user-email') ||
    document.getElementById('logout-btn')
  );
  const key = (window.getApiKey && window.getApiKey()) || '';
  if (!key && !isSessionLoggedIn) return;
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
  if (m) Modal.open(m);
};

window.closeImportModal = function() {
  const m = document.getElementById('modal-import');
  if (m) Modal.close(m);
};

function _setAttachSourceMode(mode) {
  const btnUrl = document.getElementById('btn-source-url');
  const btnFile = document.getElementById('btn-source-file');
  const panelUrl = document.getElementById('attach-source-url-panel');
  const panelFile = document.getElementById('attach-source-file-panel');

  if (mode === 'file') {
    if (btnUrl) btnUrl.classList.remove('attach-source-active');
    if (btnFile) btnFile.classList.add('attach-source-active');
    if (panelUrl) panelUrl.classList.add('attach-source-hidden');
    if (panelFile) panelFile.classList.remove('attach-source-hidden');
  } else {
    if (btnFile) btnFile.classList.remove('attach-source-active');
    if (btnUrl) btnUrl.classList.add('attach-source-active');
    if (panelFile) panelFile.classList.add('attach-source-hidden');
    if (panelUrl) panelUrl.classList.remove('attach-source-hidden');
  }
}

function _setDropzoneFileName(file) {
  const dropLabel = document.getElementById('attach-dropzone-label');
  if (!dropLabel || !file) return;
  const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
  dropLabel.textContent = '';
  const strong = document.createElement('strong');
  strong.textContent = file.name;
  dropLabel.appendChild(strong);
  dropLabel.appendChild(document.createTextNode(` (${sizeMB} MB)`));
}

let _loadRoomDaysSeq = 0;
let _loadAttachRoomsSeq = 0;

function _getResolvedEventId() {
  const attachEventSel = document.getElementById('attach-event-select');
  if (attachEventSel) {
    const opt = attachEventSel.options?.[attachEventSel.selectedIndex];
    const rawId = opt?.dataset?.eventId || attachEventSel.value;
    if (rawId) {
      const parsed = parseInt(rawId, 10);
      if (!isNaN(parsed)) return parsed;
    }
  }
  const modal = document.getElementById('modal-attach-room');
  if (modal && modal.dataset && modal.dataset.eventId) {
    const parsed = parseInt(modal.dataset.eventId, 10);
    if (!isNaN(parsed)) return parsed;
  }
  const hiddenInput = document.querySelector('input[name="event_id"]');
  if (hiddenInput && hiddenInput.value) {
    const parsed = parseInt(hiddenInput.value, 10);
    if (!isNaN(parsed)) return parsed;
  }
  return null;
}

async function _loadAttachRooms(eventId, selectedRoom = '') {
  const currentSeq = ++_loadAttachRoomsSeq;
  const roomSel = document.getElementById('attach-room-select');
  if (!roomSel) return;

  if (!eventId) {
    roomSel.replaceChildren(new Option('No rooms found', ''));
    roomSel.disabled = true;
    _loadRoomDays('');
    return;
  }

  try {
    const res = await (window.authFetch || fetch)(`/events/${eventId}/rooms`);
    if (currentSeq !== _loadAttachRoomsSeq) return;
    if (!res.ok) throw new Error('Unable to load rooms');
    const rooms = await res.json();
    if (currentSeq !== _loadAttachRoomsSeq) return;

    roomSel.replaceChildren();
    if (!Array.isArray(rooms) || rooms.length === 0) {
      roomSel.replaceChildren(new Option('No rooms found', ''));
      roomSel.disabled = true;
      _loadRoomDays('');
    } else {
      rooms.forEach(r => {
        const opt = new Option(r.name, r.name);
        roomSel.add(opt);
      });
      if (selectedRoom && [...roomSel.options].some(o => o.value === selectedRoom)) {
        roomSel.value = selectedRoom;
      } else {
        roomSel.selectedIndex = 0;
      }
      roomSel.disabled = false;
      _loadRoomDays(roomSel.value);
    }
  } catch (_) {
    if (currentSeq !== _loadAttachRoomsSeq) return;
    if (roomSel.options.length === 0) {
      roomSel.replaceChildren(new Option('No rooms found', ''));
      roomSel.disabled = true;
      _loadRoomDays('');
    } else if (roomSel.value) {
      _loadRoomDays(roomSel.value);
    }
  }
}

async function _loadRoomDays(room) {
  const currentSeq = ++_loadRoomDaysSeq;
  const dateSel = document.getElementById('attach-room-date');
  if (!dateSel) return;
  dateSel.textContent = '';
  const initialOpt = document.createElement('option');
  initialOpt.value = '';
  initialOpt.textContent = 'All Days / Select Day';
  dateSel.appendChild(initialOpt);
  if (!room || !room.trim()) return;

  const eventId = _getResolvedEventId();
  let url = `/talks/room/days?room=${encodeURIComponent(room.trim())}`;
  if (eventId) {
    url += `&event_id=${encodeURIComponent(eventId)}`;
  }

  try {
    const res = await (window.authFetch || fetch)(url);
    if (currentSeq !== _loadRoomDaysSeq) return;
    if (!res.ok) return;
    const days = await res.json();
    if (currentSeq !== _loadRoomDaysSeq) return;
    if (Array.isArray(days) && days.length > 0) {
      dateSel.textContent = '';
      if (days.length > 1) {
        const defaultOpt = document.createElement('option');
        defaultOpt.value = '';
        defaultOpt.textContent = `Select Day (${days.length} days found)`;
        dateSel.appendChild(defaultOpt);
      }
      days.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.date;
        opt.textContent = d.label;
        dateSel.appendChild(opt);
      });
      if (days.length === 1) {
        dateSel.selectedIndex = 0;
      }
    }
  } catch (e) {
    if (currentSeq === _loadRoomDaysSeq) {
      console.warn('Failed to load room days:', e);
    }
  } finally {
    if (currentSeq === _loadRoomDaysSeq) {
      _updateAttachSubmitLabel();
    }
  }
}

function _updateAttachSubmitLabel() {
  const roomSel = document.getElementById('attach-room-select') || document.getElementById('attach-room-input');
  const dateSel = document.getElementById('attach-room-date');
  const btn = document.getElementById('btn-submit-attach-room');
  if (!btn) return;

  const room = (roomSel && roomSel.value) ? roomSel.value.trim() : '';
  const selectedDateOpt = dateSel && dateSel.selectedOptions && dateSel.selectedOptions[0];
  const selectedDate = (dateSel && dateSel.value) ? dateSel.value : '';

  if (room && selectedDate && selectedDateOpt) {
    const text = selectedDateOpt.textContent;
    const dayMatch = text.match(/^(Day \d+)/);
    const dayPrefix = dayMatch ? dayMatch[1] : selectedDate;
    btn.textContent = `Attach to ${dayPrefix} Sessions (${room})`;
  } else if (room) {
    btn.textContent = `Attach to All Sessions in ${room}`;
  } else {
    btn.textContent = 'Attach Recording';
  }
}

function _initAttachRoomModal() {
  const eventId = _getResolvedEventId();
  const roomSel = document.getElementById('attach-room-select') || document.getElementById('attach-room-input');
  const currentRoom = (roomSel && roomSel.value) ? roomSel.value : '';
  if (eventId) {
    _loadAttachRooms(eventId, currentRoom);
  } else if (currentRoom) {
    _loadRoomDays(currentRoom);
  }
  _setAttachSourceMode('url');
  _updateAttachSubmitLabel();
}

window.openAttachRoomModal = function() {
  const m = document.getElementById('modal-attach-room');
  if (m) {
    Modal.open(m);
    _initAttachRoomModal();
  }
};

window.closeAttachRoomModal = function() {
  const m = document.getElementById('modal-attach-room');
  if (m) Modal.close(m);
};

window.submitAttachRoomRecording = async function() {
  const roomSelect = document.getElementById('attach-room-select') || document.getElementById('attach-room-input');
  const roomInput = roomSelect ? roomSelect.value : '';
  const dateSelect = document.getElementById('attach-room-date');
  const targetDate = dateSelect ? dateSelect.value : '';
  const urlInput = (document.getElementById('attach-room-url') || {}).value || '';
  const fileInput = document.getElementById('attach-room-file');
  const btn = document.getElementById('btn-submit-attach-room');

  const btnFile = document.getElementById('btn-source-file');
  const isFileMode = btnFile && btnFile.classList.contains('attach-source-active');

  if (!roomInput.trim()) {
    await Modal.alert({ title: 'Validation Error', message: 'Please enter or select a room name.' });
    return;
  }

  const hasFile = fileInput && fileInput.files && fileInput.files[0];
  const hasUrl = urlInput.trim().length > 0;

  if (isFileMode && !hasFile) {
    await Modal.alert({ title: 'Validation Error', message: 'Please choose or drop a video file to upload.' });
    return;
  }
  if (!isFileMode && !hasUrl) {
    await Modal.alert({ title: 'Validation Error', message: 'Please provide a valid video recording URL.' });
    return;
  }

  setBtnBusy(btn, true, 'Attaching Recording...');

  try {
    const fd = new FormData();
    fd.append('room', roomInput.trim());
    if (targetDate) {
      fd.append('target_date', targetDate);
    }
    if (isFileMode) {
      fd.append('file', fileInput.files[0]);
    } else {
      fd.append('video_url', urlInput.trim());
    }

    const resolvedEventId = _getResolvedEventId();
    if (resolvedEventId) {
      fd.append('event_id', resolvedEventId);
    }

    const res = await (window.authFetch || fetch)('/talks/room/attach-recording', {
      method: 'POST',
      body: fd,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    await Modal.alert({ title: 'Attachment Successful', message: `Successfully attached video to ${data.attached_count} session(s) in "${data.room}"!` });
    location.reload();
  } catch (err) {
    await Modal.alert({ title: 'Attachment Failed', message: `Attachment failed: ${err.message}`, variant: 'danger' });
  } finally {
    setBtnBusy(btn, false);
  }
};

window.openQuickTalkModal = function() {
  const m = document.getElementById('modal-quick-talk');
  if (m) {
    m.dataset.editId = '';
    m.dataset.eventId = getSelectedEventId() || '';
    m.dataset.roomsRequestId = '';
    m.dataset.origStart = '';
    m.dataset.origEnd = '';
    const title = m.querySelector('.v-modal-title, .dashboard-modal-title');
    const btn = document.getElementById('btn-submit-quick-talk');
    const eventSel = document.getElementById('quick-event-name');
    if (title) title.textContent = 'Register New Talk';
    if (btn) {
      btn.textContent = 'Create Talk';
      btn.dataset.origText = 'Create Talk';
    }
    if (eventSel) eventSel.parentElement.style.display = '';
    ['title', 'room', 'start', 'end', 'speaker-email'].forEach(k => {
      const el = document.getElementById(`quick-talk-${k}`);
      if (el) el.value = '';
    });
    loadQuickRooms(getSelectedEventId());
    Modal.open(m);
  }
};

window.closeQuickTalkModal = function() {
  const m = document.getElementById('modal-quick-talk');
  if (m) Modal.close(m);
};

window.openEditTalkModal = function(row) {
  const m = document.getElementById('modal-quick-talk');
  if (!m) return;
  m.dataset.editId = row.dataset.talkId;
  m.dataset.eventId = row.dataset.eventId || getSelectedEventId() || '';
  m.dataset.origStart = row.dataset.start || '';
  m.dataset.origEnd = row.dataset.end || '';
  const title = m.querySelector('.v-modal-title, .dashboard-modal-title');
  const btn = document.getElementById('btn-submit-quick-talk');
  const eventSel = document.getElementById('quick-event-name');
  if (title) title.textContent = 'Edit Talk';
  if (btn) {
    btn.textContent = 'Save Changes';
    btn.dataset.origText = 'Save Changes';
  }
  if (eventSel) eventSel.parentElement.style.display = 'none';

  const t = document.getElementById('quick-talk-title');
  if (t) t.value = row.dataset.title || '';
  const r = document.getElementById('quick-talk-room');
  loadQuickRooms(m.dataset.eventId, row.dataset.room || '');
  const sem = document.getElementById('quick-talk-speaker-email');
  if (sem) sem.value = row.dataset.speakerEmail || '';
  const toLocalInputFormat = (isoString) => {
    if (!isoString) return '';
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return '';
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };

  const s = document.getElementById('quick-talk-start');
  if (s) s.value = toLocalInputFormat(row.dataset.start);
  const e = document.getElementById('quick-talk-end');
  if (e) e.value = toLocalInputFormat(row.dataset.end);

  Modal.open(m);
};

function getSelectedEventId() {
  const eventSel = document.getElementById('quick-event-name');
  const option = eventSel?.options[eventSel.selectedIndex];
  return option?.dataset.eventId || new URLSearchParams(window.location.search).get('event_id');
}

async function loadQuickRooms(eventId, selectedRoom = '') {
  const modal = document.getElementById('modal-quick-talk');
  const select = document.getElementById('quick-talk-room');
  const addButton = document.getElementById('btn-quick-add-room');
  const requestId = `${++loadQuickRooms.requestGeneration}:${eventId || ''}`;
  if (modal) modal.dataset.roomsRequestId = requestId;
  if (!select || !eventId) {
    if (select) {
      select.replaceChildren(new Option('No room', ''));
      select.disabled = true;
    }
    if (addButton) addButton.disabled = true;
    return;
  }
  if (addButton) addButton.disabled = false;
  select.replaceChildren(new Option('Loading rooms...', ''));
  select.disabled = true;
  try {
    const response = await (window.authFetch || fetch)(`/events/${eventId}/rooms`);
    if (!response.ok) throw new Error('Unable to load rooms');
    const rooms = await response.json();
    if (modal?.dataset.roomsRequestId !== requestId) return;
    select.replaceChildren(new Option('No room', ''));
    rooms.forEach(room => select.add(new Option(room.name, room.name)));
    if (selectedRoom && ![...select.options].some(option => option.value === selectedRoom)) {
      select.add(new Option(selectedRoom, selectedRoom));
    }
    select.value = selectedRoom || '';
    select.disabled = false;
  } catch (error) {
    if (modal?.dataset.roomsRequestId !== requestId) return;
    select.replaceChildren(new Option('No room', ''));
    if (selectedRoom) select.add(new Option(selectedRoom, selectedRoom));
    select.value = selectedRoom || '';
    select.disabled = false;
  }
}

loadQuickRooms.requestGeneration = 0;

async function createQuickRoom(event) {
  event.preventDefault();
  const modal = document.getElementById('modal-quick-talk');
  const eventId = modal?.dataset.eventId || getSelectedEventId();
  if (!eventId) {
    return Modal.alert({ title: 'Add Room', message: 'Select an event before adding a room.', variant: 'danger' });
  }
  const nameInput = document.getElementById('quick-create-room-name');
  const descriptionInput = document.getElementById('quick-create-room-description');
  try {
    const response = await (window.authFetch || fetch)(`/events/${eventId}/rooms`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: nameInput.value, description: descriptionInput.value || null }),
    });
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch (_) { /* empty response */ }
      const detail = Array.isArray(body.detail) ? body.detail.map(item => item.msg || item).join(', ') : body.detail;
      return Modal.alert({ title: 'Add Room', message: detail || 'Unable to add room.', variant: 'danger' });
    }
    const room = await response.json();
    await loadQuickRooms(eventId, room.name);
    document.getElementById('quick-create-room-form').reset();
    Modal.close(document.getElementById('modal-quick-create-room'));
  } catch (_) {
    Modal.alert({ title: 'Add Room', message: 'Unable to add room.', variant: 'danger' });
  }
}

function setBtnBusy(btn, isBusy, busyText) {
  if (!btn) return;
  btn.disabled = isBusy;
  if (isBusy) {
    if (!btn.dataset.origText) btn.dataset.origText = btn.textContent.trim();
    btn.textContent = '';
    const sp = document.createElement('span');
    sp.className = 'spinner spinner-sm';
    btn.appendChild(sp);
    btn.appendChild(document.createTextNode(' ' + busyText));
  } else {
    btn.textContent = btn.dataset.origText || 'Submit';
  }
}

window.submitScheduleImport = async function() {
  const fileInput = document.getElementById('import-file-input');
  const jsonText = (document.getElementById('import-json-textarea') || {}).value || '';
  const btn = document.getElementById('btn-submit-import');

  setBtnBusy(btn, true, 'Importing...');

  try {
    let res;
    if (fileInput && fileInput.files && fileInput.files[0]) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      res = await (window.authFetch || fetch)('/talks/schedule/import', { method: 'POST', body: fd });
    } else if (jsonText.trim()) {
      let parsed;
      try { parsed = JSON.parse(jsonText); } catch { throw new Error('Invalid JSON format'); }
      res = await (window.authFetch || fetch)('/talks/schedule/import', {
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
    await Modal.alert({ title: 'Import Successful', message: `Successfully imported ${data.imported_count} session(s) into "${data.event_name}"!` });
    if (data && data.event_id) {
      window.location.href = `/studio?event_id=${data.event_id}`;
    } else {
      location.reload();
    }
  } catch (err) {
    await Modal.alert({ title: 'Import Failed', message: `Import failed: ${err.message}`, variant: 'danger' });
  } finally {
    setBtnBusy(btn, false);
  }
};

window.submitQuickTalk = async function() {
  const m = document.getElementById('modal-quick-talk');
  const editId = m ? m.dataset.editId : '';
  const eventElem = document.getElementById('quick-event-name');
  let eventName = 'General Conference';
  let eventId = null;
  if (eventElem) {
    eventName = eventElem.value || 'General Conference';
    if (eventElem.tagName === 'SELECT') {
      const opt = eventElem.options[eventElem.selectedIndex];
      if (opt && opt.dataset && opt.dataset.eventId) {
        eventId = parseInt(opt.dataset.eventId, 10);
      }
    }
  }
  const titleInput = document.getElementById('quick-talk-title');
  const title = (titleInput ? titleInput.value : '').trim();
  const rawRoom = (document.getElementById('quick-talk-room') || {}).value || '';
  const room = rawRoom.trim() || null;
  const startVal = (document.getElementById('quick-talk-start') || {}).value || '';
  const endVal = (document.getElementById('quick-talk-end') || {}).value || '';
  const speakerEmail = ((document.getElementById('quick-talk-speaker-email') || {}).value || '').trim();
  const btn = document.getElementById('btn-submit-quick-talk');

  if (!title) {
    if (titleInput) {
      titleInput.focus();
    }
    await Modal.alert({ title: 'Validation Error', message: 'Please enter a talk title.' });
    return;
  }

  if (editId) {
    const origStart = m ? m.dataset.origStart : '';
    const origEnd = m ? m.dataset.origEnd : '';
    if ((origStart && !startVal) || (origEnd && !endVal)) {
      await Modal.alert({ title: 'Validation Error', message: 'Cannot clear scheduled talk times.' });
      return;
    }
  }

  if (startVal && endVal && new Date(endVal) <= new Date(startVal)) {
    await Modal.alert({ title: 'Validation Error', message: 'End time must be after start time.' });
    return;
  }

  setBtnBusy(btn, true, editId ? 'Saving...' : 'Creating...');

  try {
    const payload = editId ? { title, room } : { event_name: eventName, title, room };
    if (speakerEmail) {
      payload.speaker_email = speakerEmail;
    } else if (editId) {
      payload.speaker_email = null;
    }

    if (!editId && eventId) {
      payload.event_id = eventId;
    }

    if (startVal) payload.start = new Date(startVal).toISOString();
    if (endVal) payload.end = new Date(endVal).toISOString();

    const url = editId ? `/talks/${editId}` : '/talks/schedule/import';
    const method = editId ? 'PATCH' : 'POST';

    const res = await (window.authFetch || fetch)(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      let msg = errData.detail || `Server returned ${res.status}`;
      if (Array.isArray(msg)) {
        msg = msg.map((e) => (e && e.msg ? e.msg : JSON.stringify(e))).join(', ');
      } else if (typeof msg === 'object' && msg !== null) {
        msg = JSON.stringify(msg);
      }
      throw new Error(msg);
    }

    if (editId) {
      window.location.reload();
      return;
    }

    const data = await res.json().catch(() => ({}));
    const targetEventId = (data && data.event_id) ? data.event_id : eventId;
    const targetUrl = targetEventId ? `/studio?event_id=${targetEventId}` : '/studio';

    if (window.location.pathname + window.location.search === targetUrl) {
      window.location.reload();
    } else {
      window.location.href = targetUrl;
    }
  } catch (err) {
    await Modal.alert({ title: 'Operation Failed', message: `Failed to ${editId ? 'edit' : 'create'} talk: ${err.message}`, variant: 'danger' });
    setBtnBusy(btn, false);
  }
};

// ── Single & Bulk Delete Operations ─────────────────────────────
window.deleteSingleTalkFromButton = function(btn) {
  if (!btn) return;
  const id = btn.getAttribute('data-talk-id');
  const title = btn.getAttribute('data-talk-title') || `Talk #${id}`;
  window.deleteSingleTalk(id, title);
};

window.deleteSingleTalk = async function(id, title) {
  const confirmed = await Modal.confirm({
    title: 'Delete Talk',
    message: `Are you sure you want to delete talk #${id}: "${title}"?\nThis will permanently delete all associated recording and media files.`,
    confirmText: 'Delete',
    variant: 'danger'
  });
  if (!confirmed) {
    return;
  }

  try {
    const res = await (window.authFetch || fetch)(`/talks/${id}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }
    location.reload();
  } catch (err) {
    await Modal.alert({ title: 'Delete Failed', message: `Delete failed: ${err.message}`, variant: 'danger' });
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

  const confirmed = await Modal.confirm({
    title: 'Delete Talks',
    message: `Are you sure you want to delete ${selected.length} selected talk(s)?\nThis will permanently delete all associated video and audio artifacts.`,
    confirmText: 'Delete All',
    variant: 'danger'
  });
  if (!confirmed) {
    return;
  }

  const btn = document.getElementById('btn-bulk-delete');
  setBtnBusy(btn, true, 'Deleting...');

  try {
    const res = await (window.authFetch || fetch)(`/talks/bulk-delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ talk_ids: selected }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server returned ${res.status}`);
    }

    const data = await res.json();
    await Modal.alert({ title: 'Delete Successful', message: `Successfully deleted ${data.deleted_count} talk(s).` });
    window.location.reload();
  } catch (err) {
    await Modal.alert({ title: 'Bulk Delete Failed', message: `Bulk delete failed: ${err.message}`, variant: 'danger' });
    setBtnBusy(btn, false);
  }
};

document.addEventListener('click', (e) => {
  const btnDel = e.target.closest('.btn-delete-talk');
  if (btnDel) {
    e.stopPropagation();
    const tid = Number(btnDel.dataset.talkId);
    const title = btnDel.dataset.talkTitle || '';
    if (tid) window.deleteSingleTalk(tid, title);
    return;
  }
  const btnEdit = e.target.closest('.btn-edit-talk');
  if (btnEdit) {
    e.stopPropagation();
    const row = btnEdit.closest('tr');
    if (row) window.openEditTalkModal(row);
    return;
  }
});

document.addEventListener('DOMContentLoaded', () => {
  const btnOpenImport = document.getElementById('btn-open-import');
  if (btnOpenImport) btnOpenImport.addEventListener('click', window.openImportModal);

  const btnOpenRoomAttach = document.getElementById('btn-open-room-attach');
  if (btnOpenRoomAttach) btnOpenRoomAttach.addEventListener('click', window.openAttachRoomModal);

  const btnOpenQuickTalk = document.getElementById('btn-open-quick-talk');
  if (btnOpenQuickTalk) btnOpenQuickTalk.addEventListener('click', window.openQuickTalkModal);

  document.querySelectorAll('.btn-close-import').forEach((b) => {
    b.addEventListener('click', window.closeImportModal);
  });
  document.querySelectorAll('.btn-close-attach-room').forEach((b) => {
    b.addEventListener('click', window.closeAttachRoomModal);
  });
  document.querySelectorAll('.btn-close-quick-talk').forEach((b) => {
    b.addEventListener('click', window.closeQuickTalkModal);
  });

  const btnSubmitImport = document.getElementById('btn-submit-import');
  if (btnSubmitImport) btnSubmitImport.addEventListener('click', window.submitScheduleImport);

  const btnSubmitAttach = document.getElementById('btn-submit-attach-room');
  if (btnSubmitAttach) btnSubmitAttach.addEventListener('click', window.submitAttachRoomRecording);

  // Attach Room Recording UI listeners
  const attachEventSelect = document.getElementById('attach-event-select');
  if (attachEventSelect) {
    attachEventSelect.addEventListener('change', () => {
      const eventId = _getResolvedEventId();
      const modal = document.getElementById('modal-attach-room');
      if (modal) {
        if (eventId) {
          modal.dataset.eventId = String(eventId);
        } else {
          delete modal.dataset.eventId;
        }
      }
      const roomSel = document.getElementById('attach-room-select');
      if (roomSel) {
        roomSel.replaceChildren(new Option('Loading rooms...', ''));
        roomSel.disabled = true;
      }
      _loadAttachRooms(eventId);
    });
  }

  const roomSelect = document.getElementById('attach-room-select');
  if (roomSelect) {
    roomSelect.addEventListener('change', () => {
      _loadRoomDays(roomSelect.value);
    });
  }

  const dateSelect = document.getElementById('attach-room-date');
  if (dateSelect) {
    dateSelect.addEventListener('change', () => {
      _updateAttachSubmitLabel();
    });
  }

  const btnSourceUrl = document.getElementById('btn-source-url');
  if (btnSourceUrl) {
    btnSourceUrl.addEventListener('click', () => _setAttachSourceMode('url'));
  }
  const btnSourceFile = document.getElementById('btn-source-file');
  if (btnSourceFile) {
    btnSourceFile.addEventListener('click', () => _setAttachSourceMode('file'));
  }

  const dropzone = document.getElementById('attach-dropzone');
  const attachFileInput = document.getElementById('attach-room-file');
  if (dropzone && attachFileInput) {
    attachFileInput.addEventListener('click', (e) => e.stopPropagation());
    dropzone.addEventListener('click', (e) => {
      if (e.target === attachFileInput) return;
      attachFileInput.click();
    });
    dropzone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        attachFileInput.click();
      }
    });

    ['dragenter', 'dragover'].forEach(eventName => {
      dropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add('drag-over');
      });
    });

    ['dragleave', 'drop'].forEach(eventName => {
      dropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('drag-over');
      });
    });

    dropzone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        attachFileInput.files = dt.files;
        _setDropzoneFileName(attachFileInput.files[0]);
      }
    });

    attachFileInput.addEventListener('change', () => {
      if (attachFileInput.files && attachFileInput.files.length) {
        _setDropzoneFileName(attachFileInput.files[0]);
      }
    });
  }

  const btnSubmitQuickTalk = document.getElementById('btn-submit-quick-talk');
  if (btnSubmitQuickTalk) btnSubmitQuickTalk.addEventListener('click', window.submitQuickTalk);
  document.getElementById('quick-event-name')?.addEventListener('change', () => {
    const modal = document.getElementById('modal-quick-talk');
    const eventId = getSelectedEventId() || '';
    if (modal && !modal.dataset.editId) modal.dataset.eventId = eventId;
    loadQuickRooms(eventId);
  });
  document.getElementById('btn-quick-add-room')?.addEventListener('click', () => Modal.open(document.getElementById('modal-quick-create-room')));
  document.getElementById('quick-create-room-form')?.addEventListener('submit', createQuickRoom);

  ['quick-talk-title', 'quick-talk-room'].forEach((id) => {
    const input = document.getElementById(id);
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          window.submitQuickTalk();
        }
      });
    }
  });

  const selectAll = document.getElementById('select-all-talks');
  if (selectAll) selectAll.addEventListener('change', () => window.toggleSelectAllTalks(selectAll));

  const btnBulkDelete = document.getElementById('btn-bulk-delete');
  if (btnBulkDelete) btnBulkDelete.addEventListener('click', window.submitBulkDelete);

  const btnClearSelection = document.getElementById('btn-clear-selection');
  if (btnClearSelection) btnClearSelection.addEventListener('click', window.clearBulkSelection);

  document.addEventListener('change', (e) => {
    if (e.target && e.target.classList && e.target.classList.contains('talk-checkbox')) {
      window.updateBulkSelectionUI();
    }
  });

  document.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-talk-id]');
    if (!row) return;
    if (
      e.target.closest('.col-checkbox') ||
      e.target.closest('.col-actions') ||
      e.target.closest('button') ||
      e.target.closest('a') ||
      e.target.closest('input')
    ) {
      return;
    }
    const talkId = row.dataset.talkId;
    if (talkId) {
      window.location = `/studio/talks/${talkId}`;
    }
  });

  // Initialize initial server-rendered job progress bars
  document.querySelectorAll('.job-progress-fill[data-progress]').forEach((el) => {
    const val = parseFloat(el.dataset.progress);
    if (!isNaN(val)) {
      el.style.width = `${Math.min(100, Math.max(0, val))}%`;
    }
  });
});


// ---------------------------------------------------------------------------
// Organizer Role Request Flow
// ---------------------------------------------------------------------------
(function initOrganizerRequest() {
  const btnRequest = document.getElementById('btn-request-organizer');
  const modal      = document.getElementById('modal-request-organizer');
  const btnClose   = document.getElementById('btn-close-request-organizer');
  const btnCancel  = document.getElementById('btn-cancel-request-organizer');
  const btnSubmit  = document.getElementById('btn-submit-request-organizer');
  const noteInput  = document.getElementById('organizer-request-note');
  const noteChars  = document.getElementById('organizer-request-note-chars');
  const errorEl    = document.getElementById('organizer-request-error');
  const banner     = document.getElementById('role-request-banner');

  if (!btnRequest || !modal) return;

  function openModal() {
    if (noteInput) noteInput.value = '';
    if (noteChars) noteChars.textContent = '0 / 500';
    if (errorEl) { errorEl.textContent = ''; errorEl.classList.add('hidden'); }
    Modal.open(modal);
    if (noteInput) noteInput.focus();
  }

  function closeModal() {
    Modal.close(modal);
  }

  btnRequest.addEventListener('click', openModal);
  if (btnClose) btnClose.addEventListener('click', closeModal);
  if (btnCancel) btnCancel.addEventListener('click', closeModal);

  if (noteInput && noteChars) {
    noteInput.addEventListener('input', () => {
      noteChars.textContent = `${noteInput.value.length} / 500`;
    });
  }

  if (btnSubmit) {
    btnSubmit.addEventListener('click', async () => {
      if (errorEl) { errorEl.textContent = ''; errorEl.classList.add('hidden'); }
      btnSubmit.disabled = true;
      btnSubmit.textContent = 'Submitting\u2026';
      try {
        const resp = await fetch('/users/request-organizer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note: noteInput ? noteInput.value.trim() : '' }),
        });
        let data = {};
        try {
          data = await resp.json();
        } catch (_) {
          data = {};
        }
        if (resp.ok) {
          closeModal();
          if (banner) {
            banner.classList.add('role-request-pending');
            banner.innerHTML = `
              <div class="role-request-icon-wrap" aria-hidden="true">
                <svg class="role-request-hero-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="10"></circle>
                  <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
              </div>
              <div class="role-request-body">
                <div class="role-request-title-row">
                  <h3 class="role-request-title">Organizer Request Pending Review</h3>
                  <span class="role-request-badge">Pending</span>
                </div>
                <p class="role-request-description">Your request for organizer access has been submitted and is pending administrator review.</p>
              </div>
            `;
          }
        } else {
          const msg = (typeof data.detail === 'string' && data.detail.trim())
            ? data.detail
            : 'Failed to submit request. Please try again.';
          if (errorEl) { errorEl.textContent = msg; errorEl.classList.remove('hidden'); }
          btnSubmit.disabled = false;
          btnSubmit.textContent = 'Submit Request';
        }
      } catch (_err) {
        if (errorEl) { errorEl.textContent = 'Network error. Please try again.'; errorEl.classList.remove('hidden'); }
        btnSubmit.disabled = false;
        btnSubmit.textContent = 'Submit Request';
      }
    });
  }
})();
