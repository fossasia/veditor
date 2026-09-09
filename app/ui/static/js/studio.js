/**
 * VEditor Studio — Interactive Timeline, Player Controller with Big Seeks, and Pipeline Review
 */

const video           = document.getElementById('main-video');
const noPreview       = document.getElementById('no-preview-msg');
const timecode        = document.getElementById('timecode-display');
const durationDisplay = document.getElementById('duration-display');
const scrubber        = document.getElementById('video-scrubber');
const speedSel        = document.getElementById('speed-select');
const jumpInput       = document.getElementById('jump-time-input');

// Seek buttons
const btnPlay         = document.getElementById('btn-play');
const iconPlay        = document.getElementById('icon-play');
const iconPause       = document.getElementById('icon-pause');
const btnSkipBack     = document.getElementById('btn-skip-back');
const btnSkipFwd      = document.getElementById('btn-skip-fwd');
const btnSeekBigBack  = document.getElementById('btn-seek-big-back');
const btnSeekBigFwd   = document.getElementById('btn-seek-big-fwd');
const btnSeekMegaBack = document.getElementById('btn-seek-mega-back');
const btnSeekMegaFwd  = document.getElementById('btn-seek-mega-fwd');
const btnPrevFrame    = document.getElementById('btn-prev-frame');
const btnNextFrame    = document.getElementById('btn-next-frame');

// Timeline elements
const tlTrack         = document.getElementById('timeline-track');
const tlStartMarker   = document.getElementById('tl-start-marker');
const tlEndMarker     = document.getElementById('tl-end-marker');
const tlPlayhead      = document.getElementById('tl-playhead');
const inputInPoint    = document.getElementById('input-in-point');
const inputOutPoint   = document.getElementById('input-out-point');
const btnSetIn        = document.getElementById('btn-set-in');
const btnSetOut       = document.getElementById('btn-set-out');
const btnPlayCut      = document.getElementById('btn-play-cut');

let inPointSec  = 0;
let outPointSec = 0;
let isPlayingCut = false;

// ── Timecode Format & Parse ─────────────────────────────────────
function formatTimecode(t) {
  if (!isFinite(t) || isNaN(t) || t < 0) return '00:00:00.00';
  const h  = Math.floor(t / 3600);
  const m  = Math.floor((t % 3600) / 60);
  const s  = Math.floor(t % 60);
  const ff = Math.floor((t % 1) * 100);
  return [h, m, s].map(v => String(v).padStart(2, '0')).join(':') +
    '.' + String(ff).padStart(2, '0');
}

function parseTimecode(str) {
  if (!str) return 0;
  const parts = str.trim().split(':');
  if (parts.length === 3) {
    const h = parseFloat(parts[0]) || 0;
    const m = parseFloat(parts[1]) || 0;
    const s = parseFloat(parts[2]) || 0;
    return h * 3600 + m * 60 + s;
  }
  if (parts.length === 2) {
    const m = parseFloat(parts[0]) || 0;
    const s = parseFloat(parts[1]) || 0;
    return m * 60 + s;
  }
  return parseFloat(str) || 0;
}

// ── Video Loading ───────────────────────────────────────────────
window.loadVideoSrc = function(url) {
  if (!video) return;
  video.pause();
  video.src = url;
  video.style.display = 'block';
  if (noPreview) noPreview.style.display = 'none';
  video.load();
  video.currentTime = 0;
  video.play().catch(() => {});

  // Highlight active row in Generated Media Assets
  document.querySelectorAll('.media-asset-row').forEach(row => {
    const rowUrl = row.getAttribute('data-asset-url');
    const btn = row.querySelector('.btn-play-asset');
    if (rowUrl === url) {
      row.style.borderColor = 'var(--v-primary)';
      row.style.background = 'var(--v-primary-subtle)';
      if (btn) {
        btn.textContent = 'Active in Studio';
        btn.classList.remove('btn-ghost');
        btn.classList.add('btn-primary');
      }
    } else {
      row.style.borderColor = 'var(--v-border-subtle)';
      row.style.background = 'var(--v-bg-subtle)';
      if (btn) {
        btn.textContent = 'Play in Studio';
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-ghost');
      }
    }
  });
};

function initInitialVideo() {
  if (typeof PREVIEW_URLS !== 'undefined' && Array.isArray(PREVIEW_URLS) && PREVIEW_URLS.length > 0) {
    window.loadVideoSrc(PREVIEW_URLS[0]);
  }
}

// ── Timecode Sync & Timeline Markers ────────────────────────────
function updateTimecode() {
  if (!video) return;
  if (timecode) timecode.textContent = formatTimecode(video.currentTime);
  const dur = video.duration || 0;

  if (dur > 0 && isFinite(dur)) {
    const pct = (video.currentTime / dur) * 100;
    if (scrubber) scrubber.value = Math.round((video.currentTime / dur) * 1000);
    if (tlPlayhead) tlPlayhead.style.left = `${pct}%`;

    if (isPlayingCut && video.currentTime >= outPointSec) {
      video.pause();
      isPlayingCut = false;
    }
  }
}

function updateTimelineTicks() {
  const dur = video.duration || 0;
  if (!dur || !isFinite(dur)) return;
  const ticks = document.getElementById('timeline-ticks');
  if (!ticks) return;
  const steps = 5;
  ticks.innerHTML = Array.from({ length: steps }, (_, i) => {
    const t = (dur / (steps - 1)) * i;
    return `<span>${formatTimecode(t).slice(0, 5)}</span>`;
  }).join('');
}

function updateCutMarkersUI() {
  const dur = video && video.duration ? video.duration : (outPointSec || 10);
  if (dur <= 0) return;

  const inPct  = Math.max(0, Math.min(100, (inPointSec / dur) * 100));
  const outPct = Math.max(0, Math.min(100, (outPointSec / dur) * 100));

  if (tlStartMarker) tlStartMarker.style.left = `${inPct}%`;
  if (tlEndMarker)   tlEndMarker.style.left   = `${outPct}%`;

  const tlContent = document.getElementById('tl-content');
  if (tlContent) {
    tlContent.style.left  = `${inPct}%`;
    tlContent.style.width = `${Math.max(0, outPct - inPct)}%`;
  }

  if (inputInPoint)  inputInPoint.value  = formatTimecode(inPointSec);
  if (inputOutPoint) inputOutPoint.value = formatTimecode(outPointSec);
}

function setInPoint(timeSec) {
  const max = (video && Number.isFinite(video.duration) && video.duration > 0) ? video.duration : Infinity;
  inPointSec = Math.min(max, Math.max(0, timeSec));
  if (inPointSec > outPointSec) outPointSec = Math.min(max, inPointSec + 1);
  updateCutMarkersUI();
}

function setOutPoint(timeSec) {
  const max = (video && Number.isFinite(video.duration) && video.duration > 0) ? video.duration : Infinity;
  outPointSec = Math.min(max, Math.max(inPointSec + 0.1, timeSec));
  updateCutMarkersUI();
}

// ── Interactive Timeline Dragging & Seeking ─────────────────────
if (tlTrack) {
  tlTrack.addEventListener('click', e => {
    if (e.target === tlStartMarker || e.target === tlEndMarker) return;
    const rect = tlTrack.getBoundingClientRect();
    const clickX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
    const pct = clickX / rect.width;
    if (video && video.duration && isFinite(video.duration)) {
      video.currentTime = pct * video.duration;
    }
  });
}

function setupMarkerDrag(markerEl, isStart) {
  if (!markerEl || !tlTrack) return;
  markerEl.addEventListener('mousedown', e => {
    e.preventDefault();
    e.stopPropagation();

    function onMouseMove(moveEvent) {
      const rect = tlTrack.getBoundingClientRect();
      const x = Math.max(0, Math.min(rect.width, moveEvent.clientX - rect.left));
      const pct = x / rect.width;
      const dur = video && video.duration ? video.duration : (outPointSec || 10);
      const timeAtCursor = pct * dur;

      if (isStart) {
        setInPoint(timeAtCursor);
      } else {
        setOutPoint(timeAtCursor);
      }
      if (video && video.duration) {
        video.currentTime = timeAtCursor;
      }
    }

    function onMouseUp() {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    }

    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
  });
}

setupMarkerDrag(tlStartMarker, true);
setupMarkerDrag(tlEndMarker, false);

// ── In/Out Buttons & Inputs ─────────────────────────────────────
if (btnSetIn) {
  btnSetIn.addEventListener('click', () => {
    if (video) setInPoint(video.currentTime);
  });
}

if (btnSetOut) {
  btnSetOut.addEventListener('click', () => {
    if (video) setOutPoint(video.currentTime);
  });
}

if (inputInPoint) {
  inputInPoint.addEventListener('change', () => {
    setInPoint(parseTimecode(inputInPoint.value));
  });
}

if (inputOutPoint) {
  inputOutPoint.addEventListener('change', () => {
    setOutPoint(parseTimecode(inputOutPoint.value));
  });
}

if (btnPlayCut) {
  btnPlayCut.addEventListener('click', () => {
    if (!video || !video.src) return;
    video.currentTime = inPointSec;
    isPlayingCut = true;
    video.play();
  });
}

// ── Jump to Timecode Input ──────────────────────────────────────
if (jumpInput) {
  jumpInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const targetSec = parseTimecode(jumpInput.value);
      if (video && isFinite(targetSec)) {
        video.currentTime = Math.max(0, Math.min(video.duration || 0, targetSec));
        jumpInput.blur();
      }
    }
  });
}

// ── Player Controls: Fine, Big & Mega Seeks ─────────────────────
function togglePlay() {
  if (!video || !video.src) return;
  if (video.paused) { video.play(); }
  else { video.pause(); }
}

function seekBy(seconds) {
  if (!video || !video.src) return;
  const target = Math.max(0, Math.min(video.duration || 0, video.currentTime + seconds));
  video.currentTime = target;
}

if (video) {
  video.addEventListener('timeupdate', updateTimecode);
  video.addEventListener('play', () => {
    if (iconPlay) iconPlay.style.display = 'none';
    if (iconPause) iconPause.style.display = 'block';
  });
  video.addEventListener('pause', () => {
    if (iconPlay) iconPlay.style.display = 'block';
    if (iconPause) iconPause.style.display = 'none';
    isPlayingCut = false;
  });
  video.addEventListener('loadedmetadata', () => {
    if (scrubber) scrubber.max = 1000;
    outPointSec = video.duration || 10;
    inPointSec = 0;
    updateTimecode();
    updateTimelineTicks();
    updateCutMarkersUI();
    if (durationDisplay) durationDisplay.textContent = `/ ${formatTimecode(video.duration)}`;
    const lbl = document.getElementById('tl-range-label');
    if (lbl) lbl.textContent = formatTimecode(video.duration);
  });
}

if (btnPlay)         btnPlay.addEventListener('click', togglePlay);
if (btnSkipBack)     btnSkipBack.addEventListener('click', () => seekBy(-5));
if (btnSkipFwd)      btnSkipFwd.addEventListener('click', () => seekBy(5));
if (btnSeekBigBack)  btnSeekBigBack.addEventListener('click', () => seekBy(-60));
if (btnSeekBigFwd)   btnSeekBigFwd.addEventListener('click', () => seekBy(60));
if (btnSeekMegaBack) btnSeekMegaBack.addEventListener('click', () => seekBy(-300));
if (btnSeekMegaFwd)  btnSeekMegaFwd.addEventListener('click', () => seekBy(300));

if (btnPrevFrame)    btnPrevFrame.addEventListener('click', () => { if(video && video.src) { video.pause(); seekBy(-1/25); } });
if (btnNextFrame)    btnNextFrame.addEventListener('click', () => { if(video && video.src) { video.pause(); seekBy(1/25); } });

if (speedSel) {
  speedSel.addEventListener('change', () => {
    if (video) video.playbackRate = parseFloat(speedSel.value);
  });
}

if (scrubber) {
  scrubber.addEventListener('input', () => {
    if (video && video.duration && isFinite(video.duration)) {
      video.currentTime = (scrubber.value / 1000) * video.duration;
    }
  });
}

// Global hotkeys
document.addEventListener('keydown', e => {
  if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;
  if (e.code === 'Space') {
    e.preventDefault();
    togglePlay();
  } else if (e.code === 'KeyI') {
    if (video) setInPoint(video.currentTime);
  } else if (e.code === 'KeyO') {
    if (video) setOutPoint(video.currentTime);
  } else if (e.code === 'ArrowLeft') {
    if (video && video.src) seekBy(e.shiftKey ? -60 : -5);
  } else if (e.code === 'ArrowRight') {
    if (video && video.src) seekBy(e.shiftKey ? 60 : 5);
  }
});

// ── Interactive Pipeline Actions ────────────────────────────────
async function postAPI(path, body = {}) {
  const res = await (window.authFetch || fetch)(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Server responded with ${res.status}`);
  }
  return res.json();
}

window.approveTalk = async function(id) {
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  const btn = document.getElementById('btn-approve');
  const originalHtml = btn ? btn.innerHTML : '';
  const talkStatus = typeof TALK_STATUS !== 'undefined' ? TALK_STATUS : '';

  if (['detecting', 'cutting', 'generating_previews', 'assembling', 'transcoding', 'uploading'].includes(talkStatus)) {
    alert(`Talk is currently processing in background (${talkStatus}). Please wait for this stage to finish.`);
    return;
  }
  if (talkStatus === 'waiting_for_files') {
    alert('Please upload a video recording before approving.');
    return;
  }

  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Processing...'; }

  const progressWrap = document.getElementById('pipeline-progress-wrap');
  const progressFill = document.getElementById('pipeline-progress-fill');
  const progressPct = document.getElementById('pipeline-progress-pct');
  const progressDesc = document.getElementById('pipeline-progress-desc');

  if (progressWrap) progressWrap.style.display = 'block';
  if (progressFill) {
    progressFill.style.width = '0%';
    progressFill.classList.add('animated');
  }
  if (progressPct) progressPct.textContent = 'Processing';
  if (progressDesc) progressDesc.textContent = 'Executing processing pipeline...';

  try {
    if (talkStatus === 'pending_approval') {
      await postAPI(`/talks/${id}/approve`, { decision: 'approve' });
    } else if (talkStatus === 'pending_bounds' || talkStatus === 'needs_work') {
      const cutStart = formatTimecode(inPointSec).slice(0, 8);
      const cutEnd = formatTimecode(outPointSec).slice(0, 8);
      await postAPI(`/talks/${id}/cut`, { cut_start: cutStart, cut_end: cutEnd });
    } else if (talkStatus === 'preview') {
      await postAPI(`/talks/${id}/review`, { decision: 'approve', note: notes || 'Approved in review studio' });
    } else if (talkStatus === 'pending_intro_outro') {
      const includeIntro = document.getElementById('check-include-intro') ? document.getElementById('check-include-intro').checked : true;
      const includeOutro = document.getElementById('check-include-outro') ? document.getElementById('check-include-outro').checked : true;
      await postAPI(`/talks/${id}/assemble`, {
        include_intro: includeIntro,
        include_outro: includeOutro,
        intro_source: 'generated',
        outro_source: 'generated',
      });

    } else {
      await postAPI(`/talks/${id}/approve`, { decision: 'approve' });
    }
    if (progressFill) progressFill.style.width = '100%';
    if (progressPct) progressPct.textContent = '100%';
    if (progressDesc) progressDesc.textContent = 'Pipeline complete! Reloading studio...';
    setTimeout(() => location.reload(), 400);
  } catch (err) {
    alert(`Pipeline action failed: ${err.message}`);
    if (progressWrap) progressWrap.style.display = 'none';
    if (btn) { btn.disabled = false; btn.innerHTML = originalHtml; }
  }
};


window.rejectTalk = async function(id) {
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  if (!confirm('Reject this talk?')) return;
  const btn = document.getElementById('btn-reject');
  const originalHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Rejecting...'; }
  try {
    const talkStatus = typeof TALK_STATUS !== 'undefined' ? TALK_STATUS : '';
    if (talkStatus === 'pending_approval') {
      await postAPI(`/talks/${id}/approve`, { decision: 'reject' });
    } else if (talkStatus === 'preview') {
      await postAPI(`/talks/${id}/review`, { decision: 'reject', note: notes || 'Rejected in review studio' });
    } else {
      await postAPI(`/talks/${id}/abort`);
    }
    location.reload();
  } catch (err) {
    alert(`Rejection failed: ${err.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = originalHtml; }
  }
};

window.requestChangesTalk = async function(id) {
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  const btn = document.getElementById('btn-needs-work');
  const originalHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Requesting changes...'; }
  try {
    await postAPI(`/talks/${id}/review`, { decision: 'needs_work', note: notes || 'Needs work' });
    location.reload();
  } catch (err) {
    alert(`Request changes failed: ${err.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = originalHtml; }
  }
};

window.retryTalk = async function(id) {
  const btn = document.getElementById('btn-retry');
  const originalHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner spinner-sm"></span> Resetting...'; }
  try {
    await postAPI(`/talks/${id}/abort`);
    location.reload();
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = originalHtml; }
  }
};

window.handleVideoFileUpload = async function(e, talkId) {
  const file = e.target.files ? e.target.files[0] : (e.dataTransfer ? e.dataTransfer.files[0] : null);
  if (!file) return;

  const progressWrap = document.getElementById('upload-progress-wrap');
  const progressText = document.getElementById('upload-progress-text');
  if (progressWrap) progressWrap.style.display = 'block';
  if (progressText) progressText.textContent = `Uploading "${file.name}" and validating streams...`;

  try {
    const fd = new FormData();
    fd.append('file', file);

    const res = await (window.authFetch || fetch)(`/talks/${talkId}/upload`, {
      method: 'POST',
      body: fd,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed with status ${res.status}`);
    }

    await res.json();
    location.reload();
  } catch (err) {
    alert(`Video upload failed: ${err.message}`);
    if (progressWrap) progressWrap.style.display = 'none';
  }
};

// ── Real-time Recent Jobs Polling & Dynamic Rendering ─────────────
function renderRecentJobs(jobs) {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!jobs || jobs.length === 0) {
    if (container) {
      container.textContent = '';
      container.style.display = 'none';
    }
    if (empty) empty.style.display = 'block';
    return;
  }

  if (empty) empty.style.display = 'none';
  if (!container) return;

  container.textContent = '';
  container.style.display = 'flex';

  jobs.forEach(job => {
    const isRunning = job.status === 'running';
    const isDone = job.status === 'done' || job.status === 'success';
    const isFailed = job.status === 'failed';
    const badgeClass = isDone ? 'badge-done' : (isRunning ? 'badge-processing' : (isFailed ? 'badge-danger' : 'badge-waiting'));

    const card = document.createElement('div');
    card.className = 'job-card';
    if (job.id) card.dataset.jobId = String(job.id);

    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;';

    const kindSpan = document.createElement('span');
    kindSpan.style.cssText = 'font-family:var(--v-font-mono);font-weight:600;';
    kindSpan.textContent = job.kind || '';

    const badgeGroup = document.createElement('div');
    badgeGroup.style.cssText = 'display:flex;align-items:center;gap:5px;';

    if (job.progress_pct !== null && job.progress_pct !== undefined && isRunning) {
      const progressBadge = document.createElement('span');
      progressBadge.className = 'badge badge-info';
      progressBadge.style.cssText = 'font-size:0.65rem;padding:1px 5px;';
      progressBadge.textContent = `${Math.round(job.progress_pct)}%`;
      badgeGroup.appendChild(progressBadge);
    }

    const statusBadge = document.createElement('span');
    statusBadge.className = `badge ${badgeClass}`;
    statusBadge.style.cssText = 'font-size:0.65rem;padding:1px 5px;';
    if (isRunning) {
      const spinner = document.createElement('span');
      spinner.className = 'spinner spinner-sm';
      statusBadge.appendChild(spinner);
    }
    statusBadge.appendChild(document.createTextNode(job.status || ''));
    badgeGroup.appendChild(statusBadge);

    header.appendChild(kindSpan);
    header.appendChild(badgeGroup);
    card.appendChild(header);

    if (isRunning && job.progress_pct !== null && job.progress_pct !== undefined) {
      const track = document.createElement('div');
      track.className = 'job-progress-track';
      const fill = document.createElement('div');
      fill.className = 'job-progress-fill animated';
      fill.style.width = `${Math.min(100, Math.max(0, job.progress_pct))}%`;
      track.appendChild(fill);
      card.appendChild(track);
    }

    if (job.started_at) {
      const d = new Date(job.started_at);
      const timeStr = !isNaN(d.getTime()) ? `${d.toISOString().slice(11, 19)} UTC` : '';
      const meta = document.createElement('div');
      meta.className = 'job-timing-meta';

      const startedSpan = document.createElement('span');
      startedSpan.textContent = `Started ${timeStr}`;
      meta.appendChild(startedSpan);

      if (isRunning && job.estimated_remaining !== null && job.estimated_remaining !== undefined) {
        const remSpan = document.createElement('span');
        remSpan.textContent = `~${Math.round(job.estimated_remaining)}s remaining`;
        meta.appendChild(remSpan);
      } else if (job.elapsed_time !== null && job.elapsed_time !== undefined) {
        const elSpan = document.createElement('span');
        elSpan.textContent = `${Math.round(job.elapsed_time)}s elapsed`;
        meta.appendChild(elSpan);
      }
      card.appendChild(meta);
    }

    container.appendChild(card);
  });
}

let studioPollInterval = null;
async function pollStudioJobs() {
  const talkId = (typeof TALK_ID !== 'undefined') ? TALK_ID : parseInt(window.location.pathname.split('/').filter(Boolean).pop(), 10);
  if (!talkId || isNaN(talkId)) return;

  try {
    const key = (window.getApiKey && window.getApiKey()) || '';
    if (!key) return;
    const headers = { 'X-API-Key': key };
    const res = await (window.authFetch || fetch)(`/studio/talks/${talkId}/jobs`, { headers, _isPolling: true });
    if (!res.ok) return;
    const data = await res.json();
    const jobs = Array.isArray(data) ? data : (data.jobs || []);
    renderRecentJobs(jobs);

    const hasRunningJob = jobs.some(j => j.status === 'running');
    const talkStatus = data.status;
    const isTerminal = ['done', 'failed', 'rejected', 'broken'].includes(talkStatus);
    if (!hasRunningJob && isTerminal && studioPollInterval) {
      clearInterval(studioPollInterval);
      studioPollInterval = null;
    }
  } catch { /* skip */ }
}

function startStudioPolling() {
  const key = (window.getApiKey && window.getApiKey()) || '';
  if (!key) return;
  if (studioPollInterval) clearInterval(studioPollInterval);
  studioPollInterval = setInterval(pollStudioJobs, 2500);
}

// ── Initial Setup & Drag-and-Drop ───────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initInitialVideo();
  updateCutMarkersUI();
  pollStudioJobs();
  startStudioPolling();

  const dropzone = document.getElementById('no-preview-msg');
  if (dropzone) {
    ['dragenter', 'dragover'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.style.borderColor = 'var(--v-primary)';
        dropzone.style.background = 'var(--v-primary-subtle)';
      });
    });

    ['dragleave', 'drop'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.style.borderColor = 'var(--v-border)';
        dropzone.style.background = '';
      });
    });

    dropzone.addEventListener('drop', (e) => {
      const talkId = window.location.pathname.split('/').filter(Boolean).pop();
      if (talkId) {
        window.handleVideoFileUpload(e, parseInt(talkId, 10));
      }
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.btn-play-asset');
    if (!btn) return;
    const url = btn.getAttribute('data-asset-url') || btn.closest('.media-asset-row')?.getAttribute('data-asset-url');
    if (url) window.loadVideoSrc(url);
  });

  // Auto-poll status when in background processing states
  const activeProcessingStates = ['detecting', 'cutting', 'generating_previews', 'assembling', 'transcoding', 'uploading'];
  const currentTalkStatus = typeof TALK_STATUS !== 'undefined' ? TALK_STATUS : '';
  const currentTalkId = window.location.pathname.split('/').filter(Boolean).pop();

  if (activeProcessingStates.includes(currentTalkStatus) && currentTalkId) {
    const pollInterval = setInterval(async () => {
      try {
        const res = await (window.authFetch || fetch)(`/talks/${currentTalkId}`, { _isPolling: true });
        if (res.ok) {
          const data = await res.json();
          if (data.status && data.status !== currentTalkStatus) {
            clearInterval(pollInterval);
            location.reload();
          }
        }
      } catch (_) {
        // Ignore network polling glitches
      }
    }, 3000);
  }
});

