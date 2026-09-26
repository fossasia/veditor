/**
 * VEditor Studio — Interactive Timeline, Player Controller with Big Seeks, and Pipeline Review
 */

function getStudioShell() {
  return document.querySelector('.studio-shell');
}

function isStudioViewOnly() {
  const shell = getStudioShell();
  return Boolean(shell && shell.classList.contains('is-view-only'));
}

function getTalkId() {
  if (typeof window.TALK_ID !== 'undefined' && window.TALK_ID) return window.TALK_ID;
  const shell = getStudioShell();
  if (shell && shell.dataset.talkId) return parseInt(shell.dataset.talkId, 10);
  const parts = window.location.pathname.split('/').filter(Boolean);
  const last = parts.pop();
  return last ? parseInt(last, 10) : null;
}

function getTalkStatus() {
  if (typeof window.TALK_STATUS !== 'undefined' && window.TALK_STATUS) return window.TALK_STATUS;
  const shell = getStudioShell();
  if (shell && shell.dataset.talkStatus) return shell.dataset.talkStatus;
  return '';
}

function getPreviewUrls() {
  if (typeof window.PREVIEW_URLS !== 'undefined' && Array.isArray(window.PREVIEW_URLS)) return window.PREVIEW_URLS;
  const shell = getStudioShell();
  if (shell && shell.dataset.previewUrls) {
    try {
      return JSON.parse(shell.dataset.previewUrls);
    } catch (_) {
      return [];
    }
  }
  return [];
}

function getActiveVideoCategory() {
  const cat = document.getElementById('media-source-select')?.selectedOptions?.[0]?.dataset?.category;
  if (cat) return cat;
  const src = (video && (video.currentSrc || video.src)) || getPreviewUrls()[0] || '';
  return src.includes('/preview/') ? 'preview' : src.includes('/final/') ? 'final' : 'raw';
}

const shellInit = getStudioShell();
if (shellInit) {
  if (typeof window.TALK_ID === 'undefined' && shellInit.dataset.talkId) {
    window.TALK_ID = parseInt(shellInit.dataset.talkId, 10);
  }
  if (typeof window.TALK_STATUS === 'undefined' && shellInit.dataset.talkStatus) {
    window.TALK_STATUS = shellInit.dataset.talkStatus;
  }
  if (typeof window.PREVIEW_URLS === 'undefined' && shellInit.dataset.previewUrls) {
    try {
      window.PREVIEW_URLS = JSON.parse(shellInit.dataset.previewUrls);
    } catch (_) {
      window.PREVIEW_URLS = [];
    }
  }
}

const video           = document.getElementById('main-video');
const noPreview       = document.getElementById('no-preview-msg');
const timecode        = document.getElementById('timecode-display');
const durationDisplay = document.getElementById('duration-display');
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
const btnMute         = document.getElementById('btn-mute');

// Timeline elements
const tlTrack         = document.getElementById('timeline-track');
const tlWaveform      = document.getElementById('tl-waveform');
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
let boundsEdited = false;
let isPlayingCut = false;
let currentWaveformPeaks = [];
let waveformAbortController = null;

const initShellBounds = getStudioShell();
if (initShellBounds) {
  const start = parseFloat(initShellBounds.dataset.cutStart) || 0;
  const end = parseFloat(initShellBounds.dataset.cutEnd) || 0;
  if (getActiveVideoCategory() === 'preview') {
    inPointSec = 0;
    outPointSec = Math.max(0, end - start);
  } else {
    inPointSec = Math.max(0, start);
    outPointSec = Math.max(inPointSec, end);
  }
}

// ── Timecode Format & Parse ─────────────────────────────────────
function formatTimecode(t) {
  if (!isFinite(t) || isNaN(t) || t < 0) return '00:00:00.00';
  const totalCs = Math.round(t * 100);
  const cs = totalCs % 100;
  const totalS = Math.floor(totalCs / 100);
  const s = totalS % 60;
  const m = Math.floor(totalS / 60) % 60;
  const h = Math.floor(totalS / 3600);
  return [h, m, s].map(v => String(v).padStart(2, '0')).join(':') +
    '.' + String(cs).padStart(2, '0');
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

// ── Audio Waveform Rendering ────────────────────────────────────
function drawWaveform() {
  if (!tlWaveform || !tlTrack) return;
  const rect = tlTrack.getBoundingClientRect();
  const width = Math.floor(rect.width);
  const height = Math.floor(rect.height);
  if (width <= 0 || height <= 0) return;

  const dpr = window.devicePixelRatio || 1;
  tlWaveform.width = width * dpr;
  tlWaveform.height = height * dpr;

  const ctx = tlWaveform.getContext('2d');
  ctx.clearRect(0, 0, tlWaveform.width, tlWaveform.height);
  if (!currentWaveformPeaks || currentWaveformPeaks.length === 0) return;

  ctx.save();
  ctx.scale(dpr, dpr);

  const waveColor = getComputedStyle(document.documentElement).getPropertyValue('--v-primary').trim() || '#2563eb';
  const count = currentWaveformPeaks.length;
  const numPoints = Math.max(30, Math.floor(width / 4));
  const bottomPadding = 2;
  const usableHeight = Math.max(2, height - bottomPadding - 4);

  const points = [];
  for (let i = 0; i <= numPoints; i++) {
    const x = (i / numPoints) * width;
    const pStart = Math.floor((i / numPoints) * count);
    const pEnd = Math.max(pStart + 1, Math.floor(((i + 1) / numPoints) * count));
    let peak = 0;
    for (let j = pStart; j < pEnd; j++) {
      if (currentWaveformPeaks[j] > peak) peak = currentWaveformPeaks[j];
    }
    const scaled = Math.pow(Math.max(0.02, Math.min(1.0, peak)), 0.72);
    points.push({ x, y: height - bottomPadding - Math.max(2, scaled * usableHeight) });
  }

  if (points.length >= 2) {
    const contour = new Path2D();
    contour.moveTo(points[0].x, points[0].y);
    for (let i = 0; i < points.length - 1; i++) {
      contour.quadraticCurveTo(points[i].x, points[i].y, (points[i].x + points[i + 1].x) / 2, (points[i].y + points[i + 1].y) / 2);
    }
    contour.lineTo(points[points.length - 1].x, points[points.length - 1].y);

    const fillPath = new Path2D(contour);
    fillPath.lineTo(width, height - bottomPadding);
    fillPath.lineTo(0, height - bottomPadding);
    fillPath.closePath();

    ctx.fillStyle = waveColor;
    ctx.globalAlpha = 0.28;
    ctx.fill(fillPath);

    ctx.strokeStyle = waveColor;
    ctx.lineWidth = 1.75;
    ctx.globalAlpha = 0.85;
    ctx.stroke(contour);
  }

  ctx.restore();
}

async function loadWaveformForUrl(videoUrl) {
  const talkId = getTalkId();
  if (!talkId || !tlWaveform) return;

  if (waveformAbortController) {
    try { waveformAbortController.abort(); } catch (_) {}
  }
  waveformAbortController = new AbortController();

  let endpoint = `/studio/talks/${talkId}/waveform`;
  if (videoUrl) {
    const match = String(videoUrl).match(/\/media\/\d+\/(?:([^/?#]+)\/)?([^/?#]+)/);
    if (match) {
      const category = match[1] || match[2].replace(/\.mp4$/i, '');
      endpoint += `?category=${encodeURIComponent(category)}&filename=${encodeURIComponent(match[2])}`;
    }
  }

  try {
    const res = await (window.authFetch || fetch)(endpoint, {
      signal: waveformAbortController.signal,
    });
    if (!res.ok) return;
    const data = await res.json();
    if (data && Array.isArray(data.peaks)) {
      currentWaveformPeaks = data.peaks;
      drawWaveform();
    }
  } catch (err) {
    if (err && err.name !== 'AbortError') {
      console.warn('Could not load waveform:', err);
    }
  }
}

function initWaveformListeners() {
  if (!tlWaveform || !tlTrack) return;
  window.addEventListener('resize', drawWaveform);
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => drawWaveform());
    ro.observe(tlTrack);
  }
  if (window.MutationObserver) {
    const mo = new MutationObserver(() => drawWaveform());
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }
}

// ── Video Loading ───────────────────────────────────────────────
window.loadVideoSrc = function(url) {
  if (!video) return;
  video.pause();
  boundsEdited = false;
  video.src = url;
  video.style.display = 'block';
  if (noPreview) noPreview.style.display = 'none';
  video.load();
  video.currentTime = 0;
  video.play().catch(() => {});
  loadWaveformForUrl(url);

  const sourceSelect = document.getElementById('media-source-select');
  if (sourceSelect && sourceSelect.value !== url) {
    sourceSelect.value = url;
  }
  const downloadBtn = document.getElementById('media-download-btn');
  if (downloadBtn && url) {
    downloadBtn.href = url;
  }
};

function initInitialVideo() {
  const sourceSelect = document.getElementById('media-source-select');
  if (sourceSelect && sourceSelect.value) {
    window.loadVideoSrc(sourceSelect.value);
    return;
  }
  const urls = getPreviewUrls();
  if (Array.isArray(urls) && urls.length > 0) {
    window.loadVideoSrc(urls[0]);
  } else {
    loadWaveformForUrl(null);
  }
}

// ── Timecode Sync & Timeline Markers ────────────────────────────
function formatSelectedDuration(sec) {
  if (!sec || sec <= 0) return '0s';
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;
}

function updateTimecode() {
  if (!video) return;
  if (timecode) timecode.textContent = formatTimecode(video.currentTime);
  const dur = video.duration || 0;

  if (dur > 0 && isFinite(dur)) {
    const pct = (video.currentTime / dur) * 100;
    if (tlPlayhead) tlPlayhead.style.left = `${pct}%`;

    if (isPlayingCut && video.currentTime >= outPointSec) {
      video.pause();
      isPlayingCut = false;
    }
  }
}

function formatTickTime(sec) {
  return new Date(Math.max(0, Math.round(sec || 0)) * 1000).toISOString().slice(11, 19);
}

function updateTimelineTicks() {
  const dur = (video && Number.isFinite(video.duration) && video.duration > 0) ? video.duration : (outPointSec || 0);
  if (!dur || dur <= 0) return;
  const ticks = document.getElementById('timeline-ticks');
  if (!ticks) return;
  const steps = 5;
  ticks.replaceChildren(
    ...Array.from({ length: steps }, (_, i) => {
      const t = (dur / (steps - 1)) * i;
      const span = document.createElement('span');
      span.className = 'tl-tick';
      span.textContent = formatTickTime(t);
      return span;
    })
  );
}

function updateCutMarkersUI() {
  const dur = video && video.duration ? video.duration : outPointSec;
  if (!dur || dur <= 0) return;

  const inPct  = Math.max(0, Math.min(100, (inPointSec / dur) * 100));
  const outPct = Math.max(0, Math.min(100, ((outPointSec || dur) / dur) * 100));

  if (tlStartMarker) tlStartMarker.style.left = `${inPct}%`;
  if (tlEndMarker)   tlEndMarker.style.left   = `${outPct}%`;

  const tlContent = document.getElementById('tl-content');
  if (tlContent) {
    tlContent.style.left  = `${inPct}%`;
    tlContent.style.width = `${Math.max(0, outPct - inPct)}%`;
  }

  if (inputInPoint)  inputInPoint.value  = formatTimecode(inPointSec);
  if (inputOutPoint) inputOutPoint.value = formatTimecode(outPointSec);

  const reviewIn = document.getElementById('review-in-point');
  const reviewOut = document.getElementById('review-out-point');
  const reviewDur = document.getElementById('review-duration');
  if (reviewIn) reviewIn.textContent = formatTimecode(inPointSec);
  if (reviewOut) reviewOut.textContent = formatTimecode(outPointSec);
  if (reviewDur) {
    const diff = Math.max(0, outPointSec - inPointSec);
    reviewDur.textContent = formatTimecode(diff);
  }
  const cutDurationBadge = document.getElementById('cut-duration-badge');
  if (cutDurationBadge) {
    const cutDuration = Math.max(0, outPointSec - inPointSec);
    cutDurationBadge.textContent = `Selected Cut: ${formatSelectedDuration(cutDuration)}`;
  }
}

function setInPoint(timeSec) {
  if (isStudioViewOnly()) return;
  const max = (video?.duration > 0) ? video.duration : Infinity;
  inPointSec = Math.max(0, Math.min(timeSec, outPointSec > 0 ? outPointSec - 0.1 : max));
  boundsEdited = true;
  updateCutMarkersUI();
}

function setOutPoint(timeSec) {
  if (isStudioViewOnly()) return;
  const max = (video?.duration > 0) ? video.duration : Infinity;
  outPointSec = Math.min(max, Math.max(timeSec, inPointSec + 0.1));
  boundsEdited = true;
  updateCutMarkersUI();
}

// ── Interactive Timeline Dragging & Seeking ─────────────────────
if (tlTrack) {
  let activeTrackPointerId = null;

  function seekTrackFromEvent(e) {
    const rect = tlTrack.getBoundingClientRect();
    const clickX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
    const pct = clickX / rect.width;
    if (video && video.duration && isFinite(video.duration)) {
      video.currentTime = pct * video.duration;
    }
  }

  tlTrack.addEventListener('pointerdown', e => {
    if (activeTrackPointerId !== null) return;
    if (tlStartMarker && (e.target === tlStartMarker || tlStartMarker.contains(e.target))) return;
    if (tlEndMarker && (e.target === tlEndMarker || tlEndMarker.contains(e.target))) return;
    activeTrackPointerId = e.pointerId;
    try { tlTrack.setPointerCapture(activeTrackPointerId); } catch (_) {}
    seekTrackFromEvent(e);
  });

  tlTrack.addEventListener('pointermove', e => {
    if (activeTrackPointerId === null || e.pointerId !== activeTrackPointerId) return;
    seekTrackFromEvent(e);
  });

  function stopTrackScrub(e) {
    if (activeTrackPointerId === null || e.pointerId !== activeTrackPointerId) return;
    try { tlTrack.releasePointerCapture(activeTrackPointerId); } catch (_) {}
    activeTrackPointerId = null;
  }

  tlTrack.addEventListener('pointerup', stopTrackScrub);
  tlTrack.addEventListener('pointercancel', stopTrackScrub);
}

function setupMarkerDrag(markerEl, isStart) {
  if (!markerEl || !tlTrack) return;
  let activePointerId = null;

  markerEl.addEventListener('pointerdown', e => {
    if (isStudioViewOnly()) return;
    if (activePointerId !== null) return;
    e.preventDefault();
    e.stopPropagation();
    activePointerId = e.pointerId;
    markerEl.setPointerCapture(activePointerId);

    function onPointerMove(ev) {
      if (ev.pointerId !== activePointerId) return;
      const rect = tlTrack.getBoundingClientRect();
      const x = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
      const pct = x / rect.width;
      const dur = video && video.duration ? video.duration : (outPointSec || 10);
      const timeAtCursor = pct * dur;

      if (isStart) setInPoint(timeAtCursor);
      else setOutPoint(timeAtCursor);
      if (video && video.duration) {
        video.currentTime = isStart ? inPointSec : outPointSec;
      }
    }

    function onPointerUp(ev) {
      if (ev.pointerId !== activePointerId) return;
      try { markerEl.releasePointerCapture(activePointerId); } catch (_) {}
      activePointerId = null;
      markerEl.removeEventListener('pointermove', onPointerMove);
      markerEl.removeEventListener('pointerup', onPointerUp);
      markerEl.removeEventListener('pointercancel', onPointerUp);
    }

    markerEl.addEventListener('pointermove', onPointerMove);
    markerEl.addEventListener('pointerup', onPointerUp);
    markerEl.addEventListener('pointercancel', onPointerUp);
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
    video.play().catch(() => {});
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
  if (video.paused) { video.play().catch(() => {}); }
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
  function initializeVideoMetadata() {
    const duration = (video?.duration > 0) ? video.duration : 10;
    if (!boundsEdited) {
      const shell = getStudioShell() || shellInit;
      const isRaw = getActiveVideoCategory() === 'raw';
      const rawStart = isRaw ? parseFloat(shell?.dataset?.cutStart) : NaN;
      const rawEnd = isRaw ? parseFloat(shell?.dataset?.cutEnd) : NaN;
      inPointSec = (!isNaN(rawStart) && rawStart >= 0) ? Math.min(rawStart, duration - 0.1) : 0;
      outPointSec = (!isNaN(rawEnd) && rawEnd > inPointSec) ? Math.min(duration, rawEnd) : duration;
    } else if (outPointSec <= 0 || outPointSec > duration) {
      outPointSec = duration;
    }
    // Seek playhead to pre-seeded inPoint so the speaker sees their talk start, not 00:00.
    if (!boundsEdited && inPointSec > 0 && video.duration > inPointSec) {
      video.currentTime = inPointSec;
    }
    updateTimecode();
    updateTimelineTicks();
    updateCutMarkersUI();
    if (durationDisplay) durationDisplay.textContent = `/ ${formatTimecode(video.duration)}`;
    const lbl = document.getElementById('tl-range-label');
    if (lbl) lbl.textContent = formatTimecode(video.duration);
    drawWaveform();
  }

  video.addEventListener('loadedmetadata', initializeVideoMetadata);

  if (video.readyState >= 1) {
    initializeVideoMetadata();
  }
  video.addEventListener('durationchange', updateTimelineTicks);
}

if (btnPlay)         btnPlay.addEventListener('click', togglePlay);
if (btnSkipBack)     btnSkipBack.addEventListener('click', () => seekBy(-5));
if (btnSkipFwd)      btnSkipFwd.addEventListener('click', () => seekBy(5));
if (btnSeekBigBack)  btnSeekBigBack.addEventListener('click', () => seekBy(-60));
if (btnSeekBigFwd)   btnSeekBigFwd.addEventListener('click', () => seekBy(60));
if (btnSeekMegaBack) btnSeekMegaBack.addEventListener('click', () => seekBy(-300));
if (btnSeekMegaFwd)  btnSeekMegaFwd.addEventListener('click', () => seekBy(300));

if (btnPrevFrame)    btnPrevFrame.addEventListener('click', () => { if(video && video.src) { video.pause(); seekBy(-0.5); } });
if (btnNextFrame)    btnNextFrame.addEventListener('click', () => { if(video && video.src) { video.pause(); seekBy(0.5); } });

function toggleMute() {
  if (!video) return;
  video.muted = !video.muted;
  updateMuteUI();
}

function updateMuteUI() {
  if (!btnMute || !video) return;
  btnMute.classList.toggle('is-muted', video.muted);
  btnMute.setAttribute('title', video.muted ? 'Unmute (M)' : 'Mute (M)');
}

if (btnMute) btnMute.addEventListener('click', toggleMute);
if (video)   video.addEventListener('volumechange', updateMuteUI);

if (speedSel) {
  speedSel.addEventListener('change', () => {
    if (video) video.playbackRate = parseFloat(speedSel.value);
  });
}

// Global hotkeys
document.addEventListener('keydown', e => {
  if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;
  if (e.code === 'Space') {
    e.preventDefault();
    togglePlay();
  } else if (e.code === 'KeyM') {
    toggleMute();
  } else if (e.code === 'KeyI') {
    if (video && btnSetIn) setInPoint(video.currentTime);
  } else if (e.code === 'KeyO') {
    if (video && btnSetOut) setOutPoint(video.currentTime);
  } else if (e.code === 'ArrowLeft') {
    if (video && video.src) seekBy(e.shiftKey ? -60 : -5);
  } else if (e.code === 'ArrowRight') {
    if (video && video.src) seekBy(e.shiftKey ? 60 : 5);
  }
});

// ── Interactive Pipeline Actions ────────────────────────────────
async function postAPI(path, body = {}, method = 'POST') {
  const res = await (window.authFetch || fetch)(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Server responded with ${res.status}`);
  }
  return res.json();
}

function setBtnBusy(btn, isBusy, busyText = 'Processing...') {
  if (!btn) return;
  btn.disabled = isBusy;
  if (isBusy) {
    if (!btn.dataset.origText) btn.dataset.origText = btn.textContent.trim();
    btn.textContent = busyText;
  } else {
    btn.textContent = btn.dataset.origText || 'Submit';
  }
}

window.approveTalk = async function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  const btn = document.getElementById('btn-approve');
  const talkStatus = getTalkStatus();

  if (['detecting', 'cutting', 'generating_previews', 'assembling', 'transcoding', 'uploading'].includes(talkStatus)) {
    alert(`Talk is currently processing in background (${talkStatus}). Please wait for this stage to finish.`);
    return;
  }
  if (talkStatus === 'waiting_for_files') {
    alert('Please upload a video recording before approving.');
    return;
  }

  setBtnBusy(btn, true, 'Processing...');

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
      const dur = (video?.duration > 0) ? video.duration : (outPointSec || 10);
      const clampedIn = Math.max(0, Math.min(inPointSec, dur - 0.1));
      const clampedOut = Math.min(dur, Math.max(outPointSec, clampedIn + 0.1));
      const base = getActiveVideoCategory() === 'preview' ? (parseFloat(getStudioShell()?.dataset?.cutStart) || 0) : 0;
      const cutPayload = { cut_start: formatTimecode(clampedIn + base), cut_end: formatTimecode(clampedOut + base) };
      if (notes) cutPayload.note = notes;
      await postAPI(`/talks/${id}/cut`, cutPayload);
    } else if (talkStatus === 'preview') {
      await postAPI(`/talks/${id}/review`, { decision: 'approve', note: notes || 'Approved in review studio' });
    } else if (talkStatus === 'pending_intro_outro') {
      const includeIntro = document.getElementById('check-include-intro') ? document.getElementById('check-include-intro').checked : false;
      const includeOutro = document.getElementById('check-include-outro') ? document.getElementById('check-include-outro').checked : false;
      const introSource = document.querySelector('input[name="intro_source"]:checked')?.value || 'generated';
      const outroSource = document.querySelector('input[name="outro_source"]:checked')?.value || 'generated';
      let customIntroPath = document.getElementById('custom-intro-path')?.value?.trim() || null;
      let customOutroPath = document.getElementById('custom-outro-path')?.value?.trim() || null;

      async function uploadCustomBumper(type, pathVal) {
        const fileInput = document.getElementById(`custom-${type}-file`);
        if (!fileInput?.files?.[0] || (pathVal && pathVal.startsWith('/'))) return pathVal;
        const fd = new FormData();
        fd.append('file', fileInput.files[0]);
        fd.append('kind', type);
        const res = await (window.authFetch || fetch)(`/talks/${id}/bumpers/upload`, {
          method: 'POST',
          body: fd,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `Failed to upload custom ${type} bumper`);
        }
        return (await res.json()).path;
      }

      if (includeIntro && introSource === 'custom') customIntroPath = await uploadCustomBumper('intro', customIntroPath);
      if (includeOutro && outroSource === 'custom') customOutroPath = await uploadCustomBumper('outro', customOutroPath);

      const speakerEmailInput = document.getElementById('handoff-speaker-email');
      const emailVal = speakerEmailInput ? speakerEmailInput.value.trim() : '';
      await postAPI(`/talks/${id}/handoff`, {
        include_intro: includeIntro,
        include_outro: includeOutro,
        intro_source: introSource,
        outro_source: outroSource,
        custom_intro_path: (includeIntro && introSource === 'custom') ? customIntroPath : null,
        custom_outro_path: (includeOutro && outroSource === 'custom') ? customOutroPath : null,
        speaker_email: emailVal || null,
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
    setBtnBusy(btn, false);
  }
};


window.abortTalk = function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const modal = document.getElementById('modal-abort');
  if (modal) {
    modal.hidden = false;
    modal.classList.add('active');
    modal.dataset.talkId = id;
  }
};

window.closeAbortModal = function() {
  const modal = document.getElementById('modal-abort');
  if (modal) {
    modal.hidden = true;
    modal.classList.remove('active');
  }
};

window.confirmAbortTalk = async function() {
  if (isStudioViewOnly()) return;
  const modal = document.getElementById('modal-abort');
  if (!modal) return;
  const id = modal.dataset.talkId;
  const btn = document.getElementById('btn-confirm-abort');
  setBtnBusy(btn, true, 'Aborting...');
  try {
    await postAPI(`/talks/${id}/abort`);
    location.reload();
  } catch (err) {
    alert(`Abort failed: ${err.message}`);
    setBtnBusy(btn, false);
    modal.classList.remove('active');
    modal.hidden = true;
  }
};

window.openResetRawModal = function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const modal = document.getElementById('modal-reset-raw');
  if (modal) {
    modal.hidden = false;
    modal.classList.add('active');
    modal.dataset.talkId = id;
  }
};

window.closeResetRawModal = function() {
  const modal = document.getElementById('modal-reset-raw');
  if (modal) {
    modal.hidden = true;
    modal.classList.remove('active');
  }
};

window.confirmResetRawTalk = async function() {
  if (isStudioViewOnly()) return;
  const modal = document.getElementById('modal-reset-raw');
  if (!modal) return;
  const id = modal.dataset.talkId || getTalkId();
  const btn = document.getElementById('btn-confirm-reset-raw');
  setBtnBusy(btn, true, 'Resetting...');
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  try {
    await postAPI(`/talks/${id}/review`, {
      decision: 'reject',
      note: notes || 'Reset talk to raw video',
    });
    location.reload();
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
    setBtnBusy(btn, false);
    modal.classList.remove('active');
    modal.hidden = true;
  }
};

window.rejectTalk = async function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const talkStatus = getTalkStatus();
  if (talkStatus === 'preview') {
    window.openResetRawModal(id);
    return;
  }
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  if (!confirm('Reject this talk?')) return;
  const btn = document.getElementById('btn-reject');
  setBtnBusy(btn, true, 'Rejecting...');
  try {
    if (talkStatus === 'pending_approval') {
      await postAPI(`/talks/${id}/approve`, { decision: 'reject' });
    } else {
      await postAPI(`/talks/${id}/abort`);
    }
    location.reload();
  } catch (err) {
    alert(`Reject failed: ${err.message}`);
    setBtnBusy(btn, false);
  }
};

window.requestChangesTalk = async function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const notes = (document.getElementById('review-notes-input') || {}).value || '';
  const btn = document.getElementById('btn-needs-work');
  setBtnBusy(btn, true, 'Requesting changes...');
  try {
    await postAPI(`/talks/${id}/review`, { decision: 'needs_work', note: notes || 'Needs work' });
    location.reload();
  } catch (err) {
    alert(`Request changes failed: ${err.message}`);
    setBtnBusy(btn, false);
  }
};

window.retryTalk = async function(id) {
  if (isStudioViewOnly()) return;
  if (!id || typeof id !== 'number') id = getTalkId();
  const btn = document.getElementById('btn-retry');
  setBtnBusy(btn, true, 'Resetting...');
  try {
    await postAPI(`/talks/${id}/abort`);
    location.reload();
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
    setBtnBusy(btn, false);
  }
};
window.handleVideoFileUpload = async function(e, talkId) {
  if (isStudioViewOnly()) return;
  if (!talkId || typeof talkId !== 'number') talkId = getTalkId();
  const file = e.target.files ? e.target.files[0] : (e.dataTransfer ? e.dataTransfer.files[0] : null);
  if (!file) return;

  const progressWrap = document.getElementById('upload-progress-wrap');
  const progressText = document.getElementById('upload-progress-text');
  const browseBtn = document.getElementById('btn-browse-file');
  if (progressWrap) progressWrap.style.display = 'block';
  if (progressText) progressText.textContent = `Uploading "${file.name}"...`;
  if (browseBtn) browseBtn.disabled = true;

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

    if (progressText) progressText.textContent = 'Uploaded! Ingesting & validating video streams...';

    const pollIngest = setInterval(async () => {
      try {
        const checkRes = await (window.authFetch || fetch)(`/talks/${talkId}`, { _isPolling: true });
        if (checkRes.ok) {
          const talkData = await checkRes.json();
          if (talkData.status && talkData.status !== 'waiting_for_files') {
            clearInterval(pollIngest);
            location.reload();
          }
        }
      } catch (_) {}
    }, 1000);
  } catch (err) {
    alert(`Video upload failed: ${err.message}`);
    if (progressWrap) progressWrap.style.display = 'none';
    if (browseBtn) browseBtn.disabled = false;
  }
};

// ── Admin View-Only Mode Handling ───────────────────────────────
function initAdminViewOnlyMode() {
  const shell = getStudioShell();
  if (!shell || shell.dataset.isOtherOrganizer !== 'true') return;

  const sessionKey = 'studio_edit_unlocked_' + getTalkId();
  const banner = document.getElementById('admin-view-only-banner');
  const badge = document.getElementById('banner-mode-badge');
  const desc = document.getElementById('banner-mode-text');
  const toggleBtn = document.getElementById('btn-toggle-edit-mode');
  const modal = document.getElementById('admin-confirm-edit-modal');
  const toggleModal = show => { if (modal) modal.hidden = !show; };

  function applyLockState(unlocked) {
    shell.classList.toggle('is-view-only', !unlocked);
    if (banner) banner.classList.toggle('is-edit-active', unlocked);
    if (badge) {
      badge.className = unlocked ? 'badge badge-success' : 'badge badge-warning';
      badge.textContent = unlocked ? 'Edit Mode Active' : 'View Only';
    }
    if (desc) {
      if (unlocked) {
        desc.textContent =
          'Edit mode enabled. You can now modify cut bounds, upload recordings, and execute pipeline actions.';
      } else if (desc.dataset.defaultText) {
        const eventName = desc.dataset.eventName;
        if (eventName) {
          desc.textContent = "Viewing another organizer's talk (";
          const strong = document.createElement('strong');
          strong.textContent = eventName;
          desc.appendChild(strong);
          desc.appendChild(document.createTextNode('). Editing and pipeline actions are locked.'));
        } else {
          desc.textContent = desc.dataset.defaultText;
        }
      }
    }
    if (toggleBtn) {
      toggleBtn.className = unlocked ? 'btn btn-ghost btn-sm' : 'btn btn-warning btn-sm';
      toggleBtn.innerHTML = unlocked
        ? '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> <span>Lock (View Only)</span>'
        : '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg> <span>Enable Edit Mode</span>';
    }
    const editControlsSelector =
      '[data-edit-control], .upload-pending-container input, .upload-pending-container button, .timeline-inputs-bar input, .timeline-inputs-bar button, .review-box input, .review-box button, .review-box textarea, .studio-panel-body input, .studio-panel-body button, .studio-panel-body textarea';
    shell.querySelectorAll(editControlsSelector).forEach(el => {
      if (
        el.closest('#admin-view-only-banner') ||
        el.closest('#admin-confirm-edit-modal') ||
        el.closest('.studio-panel-header') ||
        el.id === 'btn-toggle-right-panel' ||
        el.id === 'btn-expand-right-panel'
      ) {
        return;
      }
      if (unlocked) {
        if (el.dataset.viewLocked === 'true') {
          el.disabled = el.dataset.operationalDisabled === 'true';
          if (el.tagName === 'TEXTAREA' || el.type === 'text') {
            el.readOnly = el.dataset.operationalReadOnly === 'true';
            delete el.dataset.operationalReadOnly;
          }
          delete el.dataset.operationalDisabled;
          delete el.dataset.viewLocked;
        }
      } else {
        if (el.dataset.viewLocked !== 'true') {
          el.dataset.viewLocked = 'true';
          el.dataset.operationalDisabled = el.disabled ? 'true' : 'false';
          if (el.tagName === 'TEXTAREA' || el.type === 'text') {
            el.dataset.operationalReadOnly = el.readOnly ? 'true' : 'false';
          }
        }
        el.disabled = true;
        if (el.tagName === 'TEXTAREA' || el.type === 'text') {
          el.readOnly = true;
        }
      }
    });
  }

  applyLockState(sessionStorage.getItem(sessionKey) === 'true');

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      if (sessionStorage.getItem(sessionKey) === 'true') {
        sessionStorage.removeItem(sessionKey);
        applyLockState(false);
      } else {
        toggleModal(true);
      }
    });
  }

  const cancelBtn = document.getElementById('btn-modal-cancel');
  const closeXBtn = document.getElementById('btn-modal-close-x');
  const confirmBtn = document.getElementById('btn-modal-confirm');

  if (cancelBtn) cancelBtn.addEventListener('click', () => toggleModal(false));
  if (closeXBtn) closeXBtn.addEventListener('click', () => toggleModal(false));
  if (modal) modal.addEventListener('click', e => { if (e.target === modal) toggleModal(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && modal && !modal.hidden) toggleModal(false); });

  if (confirmBtn) {
    confirmBtn.addEventListener('click', () => {
      sessionStorage.setItem(sessionKey, 'true');
      toggleModal(false);
      applyLockState(true);
    });
  }
}

// ── Real-time Recent Jobs Polling & Dynamic Rendering ─────────────
function renderRecentJobs(jobs) {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!container && !empty) return;
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

// ── Collapsible Right Sidebar ────────────────────────────────────
function initRightPanelCollapse() {
  const shell = getStudioShell();
  const btnToggle = document.getElementById('btn-toggle-right-panel');
  const btnExpand = document.getElementById('btn-expand-right-panel');

  function setCollapsed(collapsed, shiftFocus = false) {
    if (!shell) return;
    shell.classList.toggle('right-panel-collapsed', collapsed);
    if (btnToggle) btnToggle.setAttribute('aria-expanded', String(!collapsed));
    if (btnExpand) btnExpand.setAttribute('aria-expanded', String(!collapsed));
    if (shiftFocus) (collapsed ? btnExpand : btnToggle)?.focus();
    try {
      localStorage.setItem('veditor_right_panel_collapsed', String(collapsed));
    } catch (_) {}
  }

  try {
    const isCollapsed = localStorage.getItem('veditor_right_panel_collapsed') === 'true';
    if (isCollapsed) setCollapsed(true, false);
  } catch (_) {}

  if (btnToggle) {
    btnToggle.addEventListener('click', () => setCollapsed(true, true));
  }
  if (btnExpand) {
    btnExpand.addEventListener('click', () => setCollapsed(false, true));
  }
}

// ── Bumper Studio Controls ───────────────────────────────────────
function initBumperStudio() {
  ['intro', 'outro'].forEach(type => {
    const toggle = document.getElementById(`check-include-${type}`);
    const wrap = document.getElementById(`${type}-options-wrap`);
    if (toggle && wrap) {
      toggle.addEventListener('change', () => wrap.classList.toggle('is-hidden', !toggle.checked));
    }

    const radios = document.querySelectorAll(`input[name="${type}_source"]`);
    const customWrap = document.getElementById(`${type}-custom-wrap`);
    const file = document.getElementById(`custom-${type}-file`);
    const path = document.getElementById(`custom-${type}-path`);
    const dropzone = document.getElementById(`${type}-dropzone`);
    const fileName = document.getElementById(`${type}-file-name`);
    let currentUploadGen = 0;

    radios.forEach(r => {
      r.addEventListener('change', () => {
        radios.forEach(rad => rad.closest('.bumper-segment')?.classList.toggle('is-active', rad.checked));
        if (customWrap) customWrap.classList.toggle('is-visible', r.value === 'custom' && r.checked);
      });
    });

    async function handleSelectedFile(selected) {
      if (!selected) return;
      const uploadGen = ++currentUploadGen;
      if (fileName) {
        fileName.textContent = `${selected.name} (Uploading...)`;
        fileName.classList.remove('is-ready');
      }
      if (path) path.value = selected.name;
      try {
        const fd = new FormData();
        fd.append('file', selected);
        fd.append('kind', type);
        const res = await (window.authFetch || fetch)(`/talks/${getTalkId()}/bumpers/upload`, {
          method: 'POST',
          body: fd,
        });
        if (uploadGen !== currentUploadGen) return;
        if (res.ok) {
          const data = await res.json();
          if (uploadGen !== currentUploadGen) return;
          if (data.path) {
            if (path) path.value = data.path;
            if (fileName) {
              fileName.textContent = `${selected.name} ✓`;
              fileName.classList.add('is-ready');
            }
          }
        }
      } catch (_) {
        if (uploadGen === currentUploadGen && fileName) {
          fileName.textContent = selected.name;
        }
      }
    }

    if (file) {
      file.addEventListener('change', () => {
        const selected = file.files?.[0];
        if (selected) handleSelectedFile(selected);
      });
    }

    if (dropzone) {
      ['dragenter', 'dragover'].forEach(name => {
        dropzone.addEventListener(name, (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.add('drag-over');
        });
      });
      ['dragleave', 'drop'].forEach(name => {
        dropzone.addEventListener(name, (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.remove('drag-over');
        });
      });
      dropzone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const dropped = dt?.files?.[0];
        if (dropped) {
          if (file) {
            try {
              file.files = dt.files;
            } catch (_) {}
          }
          handleSelectedFile(dropped);
        }
      });
    }

    if (path?.value && fileName && path.value.trim() !== '') {
      fileName.classList.add('is-ready');
    }
  });
}

let studioPollInterval = null;
async function pollStudioJobs() {
  const talkId = getTalkId();
  if (!talkId || isNaN(talkId)) return;

  try {
    const res = await (window.authFetch || fetch)(`/talks/${talkId}/jobs`, { _isPolling: true });
    if (!res.ok) return;
    const data = await res.json();
    const jobs = Array.isArray(data) ? data : (data.jobs || []);
    renderRecentJobs(jobs);

    const currentStatus = getTalkStatus();
    if (data.status && data.status !== currentStatus) {
      location.reload();
      return;
    }

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
  if (studioPollInterval) clearInterval(studioPollInterval);
  studioPollInterval = setInterval(pollStudioJobs, 2500);
}

// ── Initial Setup & Event Listeners ─────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initAdminViewOnlyMode();

  // Sync progress bars width from data-progress attribute
  document.querySelectorAll('.job-progress-fill[data-progress]').forEach(el => {
    const p = parseFloat(el.getAttribute('data-progress'));
    if (!isNaN(p)) el.style.width = `${Math.min(100, Math.max(0, p))}%`;
  });

  initInitialVideo();
  initWaveformListeners();
  updateCutMarkersUI();
  initRightPanelCollapse();
  initBumperStudio();
  pollStudioJobs();
  startStudioPolling();

  const sourceSelect = document.getElementById('media-source-select');
  const downloadBtn = document.getElementById('media-download-btn');
  if (sourceSelect) {
    if (downloadBtn && sourceSelect.value) {
      downloadBtn.href = sourceSelect.value;
    }
    sourceSelect.addEventListener('change', () => {
      const url = sourceSelect.value;
      if (url) {
        window.loadVideoSrc(url);
      }
    });
  }

  const videoInput = document.getElementById('video-file-input');
  if (videoInput) {
    videoInput.addEventListener('change', (e) => {
      window.handleVideoFileUpload(e, getTalkId());
    });
  }

  const dropzone = document.getElementById('no-preview-msg');
  if (dropzone) {
    dropzone.addEventListener('click', (e) => {
      if (e.target.closest('#btn-browse-file') || e.target === videoInput) return;
      if (videoInput) videoInput.click();
    });

    ['dragenter', 'dragover'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add('drag-over');
      });
    });

    ['dragleave', 'drop'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('drag-over');
      });
    });

    dropzone.addEventListener('drop', (e) => {
      const talkId = getTalkId();
      if (talkId) {
        window.handleVideoFileUpload(e, talkId);
      }
    });
  }

  const btnBrowse = document.getElementById('btn-browse-file');
  if (btnBrowse && videoInput) {
    btnBrowse.addEventListener('click', (e) => {
      e.stopPropagation();
      videoInput.click();
    });
  }

  const actionBtns = document.getElementById('action-btns');
  if (actionBtns) {
    actionBtns.addEventListener('click', (e) => {
      const proceedBtn = e.target.closest('#btn-proceed-handoff');
      const backBtn = e.target.closest('#btn-back-bumpers');
      if (proceedBtn || backBtn) {
        document.getElementById('card-bumper-options')?.classList.toggle('is-hidden', !!proceedBtn);
        document.getElementById('card-speaker-handoff')?.classList.toggle('is-hidden', !proceedBtn);
        return;
      }
      const approveBtn = e.target.closest('#btn-approve');
      if (approveBtn) {
        window.approveTalk(getTalkId());
        return;
      }
      const rejectBtn = e.target.closest('#btn-reject');
      if (rejectBtn) {
        window.rejectTalk(getTalkId());
        return;
      }
      const needsWorkBtn = e.target.closest('#btn-needs-work');
      if (needsWorkBtn) {
        window.requestChangesTalk(getTalkId());
        return;
      }
      const abortBtn = e.target.closest('#btn-abort');
      if (abortBtn) {
        window.abortTalk(getTalkId());
        return;
      }
    });
  }

  document.addEventListener('click', (e) => {
    const closeBtn = e.target.closest('.btn-close-modal, [id^="btn-close-"], [id^="btn-cancel-"]');
    if (closeBtn || e.target.classList.contains('modal-backdrop')) {
      const targetModal = closeBtn ? closeBtn.closest('.modal-backdrop') : e.target;
      if (targetModal) {
        targetModal.classList.remove('active');
        targetModal.hidden = true;
      }
      return;
    }
    if (e.target.closest('#btn-confirm-abort')) return window.confirmAbortTalk();
    if (e.target.closest('#btn-confirm-reset-raw')) return window.confirmResetRawTalk();
    const btn = e.target.closest('.btn-play-asset');
    if (!btn) return;
    const url = btn.getAttribute('data-asset-url') || btn.closest('.media-asset-row')?.getAttribute('data-asset-url');
    if (url) window.loadVideoSrc(url);
  });

  // Auto-poll status when in background processing states
  const activeProcessingStates = ['detecting', 'cutting', 'generating_previews', 'assembling', 'transcoding', 'uploading'];
  const currentTalkStatus = getTalkStatus();
  const currentTalkId = getTalkId();

  const editBtn = document.getElementById('btn-edit-talk-studio');
  const editModal = document.getElementById('modal-edit-talk-studio');
  if (editBtn && editModal) {
    editBtn.addEventListener('click', () => {
      const shell = getStudioShell();
      if (!shell) return;
      document.getElementById('edit-talk-title').value = shell.dataset.talkTitle || '';
      document.getElementById('edit-talk-room').value = shell.dataset.talkRoom || '';
      const toLocalInputFormat = (isoString) => {
        if (!isoString) return '';
        const d = new Date(isoString);
        if (isNaN(d.getTime())) return '';
        return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      };
      
      document.getElementById('edit-talk-start').value = toLocalInputFormat(shell.dataset.talkStart);
      document.getElementById('edit-talk-end').value = toLocalInputFormat(shell.dataset.talkEnd);
      editModal.style.display = 'flex';
    });

    editModal.querySelectorAll('.btn-close-edit-talk, .dashboard-modal-close').forEach(b => {
      b.addEventListener('click', () => { editModal.style.display = 'none'; });
    });
    editModal.addEventListener('click', e => {
      if (e.target === editModal) editModal.style.display = 'none';
    });

    const submitBtn = document.getElementById('btn-submit-edit-talk-studio');
    if (submitBtn) {
      submitBtn.addEventListener('click', async () => {
        const title = document.getElementById('edit-talk-title').value.trim();
        const rawRoom = (document.getElementById('edit-talk-room') || {}).value || '';
        const room = rawRoom.trim() || null;
        const startVal = document.getElementById('edit-talk-start').value;
        const endVal = document.getElementById('edit-talk-end').value;

        const shell = getStudioShell();
        const origStart = shell ? shell.dataset.talkStart : '';
        const origEnd = shell ? shell.dataset.talkEnd : '';

        if (!title) {
          alert('Talk title is required.');
          return;
        }
        if ((origStart && !startVal) || (origEnd && !endVal)) {
          alert('Cannot clear scheduled talk times.');
          return;
        }
        if (startVal && endVal && new Date(endVal) <= new Date(startVal)) {
          alert('End time must be after start time.');
          return;
        }

        const origText = submitBtn.textContent;
        submitBtn.disabled = true;
        submitBtn.textContent = '';
        const sp = document.createElement('span');
        sp.className = 'spinner spinner-sm';
        submitBtn.appendChild(sp);
        submitBtn.appendChild(document.createTextNode(' Saving...'));

        try {
          const payload = { title, room };
          if (startVal) payload.start = new Date(startVal).toISOString();
          if (endVal) payload.end = new Date(endVal).toISOString();

          const res = await (window.authFetch || fetch)(`/talks/${currentTalkId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload),
          });

          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `Server returned ${res.status}`);
          }
          window.location.reload();
        } catch (err) {
          alert(`Failed to update talk: ${err.message}`);
          submitBtn.disabled = false;
          submitBtn.textContent = origText;
        }
      });
    }
  }

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
