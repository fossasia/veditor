/**
 * VEditor Minimal Dashboard & Step-by-Step Pipeline Studio
 * Communicates with VEditor REST API & Pipeline execution endpoints.
 */

document.addEventListener('DOMContentLoaded', () => {
  // State
  let currentApiKey = localStorage.getItem('veditor_api_key') || 'test-client-key';
  let allTalks = [];
  let currentFilter = 'all';
  let activeTalk = null;
  let inPointSeconds = 2.0;
  let outPointSeconds = 12.0;
  let currentVideoSrc = '/data/1/raw/sample-3.mp4';
  let jobPollingInterval = null;

  // Fallback demo talks if database is unseeded/offline
  const DEMO_TALKS = [
    {
      id: 1,
      title: "Building High-Throughput Media Pipelines with PyAV",
      speakers: ["Saalim", "ViRUS-0-0"],
      event_id: 101,
      room_date: "Hall 1 • Main Stage",
      state: "pending_approval",
      start_seconds: 2.0,
      end_seconds: 14.5,
      raw_video_path: "/data/1/raw/sample-3.mp4"
    },
    {
      id: 2,
      title: "Automated Loudness Normalization & Multi-Format Transcoding",
      speakers: ["Jane Smith"],
      event_id: 101,
      room_date: "Room B • Track 2",
      state: "cutting",
      start_seconds: 0.0,
      end_seconds: 18.0,
      raw_video_path: "/data/1/raw/sample-3.mp4"
    },
    {
      id: 3,
      title: "Keynote: Next-Gen Eventyay Video Architecture",
      speakers: ["Mario Behling", "Hong Phuc Dang"],
      event_id: 102,
      room_date: "Auditorium",
      state: "done",
      start_seconds: 0.0,
      end_seconds: 25.0,
      raw_video_path: "/data/1/raw/sample-3.mp4"
    }
  ];

  // DOM Elements
  const apiKeyInput = document.getElementById('apiKeyInput');
  const saveApiKeyBtn = document.getElementById('saveApiKeyBtn');
  const refreshTalksBtn = document.getElementById('refreshTalksBtn');
  const talksTableBody = document.getElementById('talksTableBody');
  const searchTalksInput = document.getElementById('searchTalksInput');
  const statusFilterTabs = document.getElementById('statusFilterTabs');

  // Stats Elements
  const statTotalTalks = document.getElementById('statTotalTalks');
  const statPending = document.getElementById('statPending');
  const statProcessing = document.getElementById('statProcessing');
  const statDone = document.getElementById('statDone');

  // Studio Elements
  const cutterModal = document.getElementById('cutterModal');
  const closeCutterModalBtn = document.getElementById('closeCutterModalBtn');
  const modalTalkTitle = document.getElementById('modalTalkTitle');
  const modalTalkEventBadge = document.getElementById('modalTalkEventBadge');
  const previewVideo = document.getElementById('previewVideo');
  const tcCurrent = document.getElementById('tcCurrent');
  const tcStart = document.getElementById('tcStart');
  const tcEnd = document.getElementById('tcEnd');
  const tcDuration = document.getElementById('tcDuration');
  const setStartBtn = document.getElementById('setStartBtn');
  const setEndBtn = document.getElementById('setEndBtn');
  const playCutRangeBtn = document.getElementById('playCutRangeBtn');
  const resetVideoBtn = document.getElementById('resetVideoBtn');
  const stepResultBanner = document.getElementById('stepResultBanner');
  const stepResultText = document.getElementById('stepResultText');

  // Step Tabs
  const pipelineStepTabs = document.getElementById('pipelineStepTabs');
  const stepContents = document.querySelectorAll('.step-content');

  // Step 1 Elements
  const runDetectBtn = document.getElementById('runDetectBtn');
  const detectSummary = document.getElementById('detectSummary');
  const forceReencodeCheckbox = document.getElementById('forceReencodeCheckbox');
  const runCutBtn = document.getElementById('runCutBtn');

  // Step 2 Elements
  const targetLufsInput = document.getElementById('targetLufsInput');
  const runLoudnessBtn = document.getElementById('runLoudnessBtn');
  const lufsPresetBtns = document.querySelectorAll('.lufs-preset-btn');

  // Step 3 Elements
  const introEventInput = document.getElementById('introEventInput');
  const introRoomInput = document.getElementById('introRoomInput');
  const introTitleInput = document.getElementById('introTitleInput');
  const introSpeakersInput = document.getElementById('introSpeakersInput');
  const runIntroBtn = document.getElementById('runIntroBtn');
  const outroThankYouInput = document.getElementById('outroThankYouInput');
  const outroLinksInput = document.getElementById('outroLinksInput');
  const runOutroBtn = document.getElementById('runOutroBtn');

  // Step 4 Elements
  const previewPresetSelect = document.getElementById('previewPresetSelect');
  const runPreviewBtn = document.getElementById('runPreviewBtn');

  // Step 5 Elements
  const transcodePresetSelect = document.getElementById('transcodePresetSelect');
  const runTranscodeBtn = document.getElementById('runTranscodeBtn');

  // Job Monitor
  const jobProgressModal = document.getElementById('jobProgressModal');
  const closeJobModalBtn = document.getElementById('closeJobModalBtn');
  const jobMonitorId = document.getElementById('jobMonitorId');
  const jobMonitorStatus = document.getElementById('jobMonitorStatus');
  const jobProgressBar = document.getElementById('jobProgressBar');
  const jobProgressPct = document.getElementById('jobProgressPct');

  // Init API Key Input
  apiKeyInput.value = currentApiKey;

  saveApiKeyBtn.addEventListener('click', () => {
    currentApiKey = apiKeyInput.value.trim();
    localStorage.setItem('veditor_api_key', currentApiKey);
    fetchTalks();
  });

  refreshTalksBtn.addEventListener('click', fetchTalks);

  // Status Filter Tabs
  statusFilterTabs.addEventListener('click', (e) => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    renderTalks();
  });

  // Search Input
  searchTalksInput.addEventListener('input', renderTalks);

  // Step Tabs Navigation
  pipelineStepTabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.step-tab');
    if (!tab) return;
    document.querySelectorAll('.step-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const stepNum = tab.dataset.step;
    stepContents.forEach(content => {
      content.classList.remove('active');
      if (content.id === `step${stepNum}Content`) {
        content.classList.add('active');
      }
    });
  });

  // Modal Closers
  closeCutterModalBtn.addEventListener('click', () => {
    previewVideo.pause();
    cutterModal.classList.remove('active');
  });

  closeJobModalBtn.addEventListener('click', () => {
    if (jobPollingInterval) clearInterval(jobPollingInterval);
    jobProgressModal.classList.remove('active');
  });

  // Video Timecode Handling
  previewVideo.addEventListener('timeupdate', () => {
    const cur = previewVideo.currentTime;
    tcCurrent.textContent = formatTimecode(cur);

    if (previewVideo.dataset.playingCutRange === 'true' && cur >= outPointSeconds) {
      previewVideo.pause();
      previewVideo.dataset.playingCutRange = 'false';
    }
  });

  previewVideo.addEventListener('loadedmetadata', () => {
    if (outPointSeconds === 0 || outPointSeconds > previewVideo.duration) {
      outPointSeconds = previewVideo.duration || 10.0;
    }
    updateTimecodeUI();
  });

  setStartBtn.addEventListener('click', () => {
    inPointSeconds = previewVideo.currentTime;
    if (inPointSeconds > outPointSeconds) {
      outPointSeconds = previewVideo.duration || inPointSeconds;
    }
    updateTimecodeUI();
  });

  setEndBtn.addEventListener('click', () => {
    outPointSeconds = previewVideo.currentTime;
    if (outPointSeconds < inPointSeconds) {
      inPointSeconds = 0;
    }
    updateTimecodeUI();
  });

  playCutRangeBtn.addEventListener('click', () => {
    previewVideo.currentTime = inPointSeconds;
    previewVideo.dataset.playingCutRange = 'true';
    previewVideo.play();
  });

  resetVideoBtn.addEventListener('click', () => {
    loadVideo('/data/1/raw/sample-3.mp4');
    showResultBanner('Reset player to original raw video: sample-3.mp4');
  });

  // COMPLETE END-TO-END PIPELINE ORCHESTRATION
  const runFullPipelineBtn = document.getElementById('runFullPipelineBtn');
  if (runFullPipelineBtn) {
    runFullPipelineBtn.addEventListener('click', async () => {
      runFullPipelineBtn.disabled = true;
      runFullPipelineBtn.textContent = 'Running Full Pipeline...';
      openMultiStageJobMonitor('End-to-End Pipeline Assembly');

      try {
        const speakersList = introSpeakersInput.value.split(',').map(s => s.trim()).filter(Boolean);
        const resp = await fetch('/api/pipeline/run-full-pipeline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            input_path: 'data/1/raw/sample-3.mp4',
            start_seconds: inPointSeconds,
            end_seconds: outPointSeconds,
            target_lufs: parseFloat(targetLufsInput.value) || -16.0,
            include_intro: true,
            intro_title: introTitleInput.value,
            intro_speakers: speakersList,
            intro_event: introEventInput.value,
            intro_room: introRoomInput.value,
            include_outro: true,
            outro_text: outroThankYouInput.value,
            outro_links: outroLinksInput.value,
            preset_name: transcodePresetSelect.value || '1080p_default'
          })
        });

        const res = await resp.json();
        if (resp.ok) {
          loadVideo(res.master_url);
          showResultBanner('🎉 Complete Pipeline Assembly Finished! Stitched [Intro] + [Cut Talk] + [Outro] into master presentation video.');
        } else {
          alert(`Pipeline execution error: ${res.detail || 'Failed'}`);
        }
      } catch (err) {
        alert(`Pipeline request error: ${err.message}`);
      } finally {
        runFullPipelineBtn.disabled = false;
        runFullPipelineBtn.textContent = 'Run Full Pipeline';
      }
    });
  }

  // STEP 1: Auto-Detect Execution
  runDetectBtn.addEventListener('click', async () => {
    runDetectBtn.disabled = true;
    runDetectBtn.textContent = 'Detecting speech boundaries...';

    try {
      const resp = await fetch('/api/pipeline/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input_path: 'data/1/raw/sample-3.mp4' })
      });

      const res = await resp.json();
      if (resp.ok && res.segments && res.segments.length > 0) {
        const speechSegs = res.segments.filter(s => s.is_speech);
        if (speechSegs.length > 0) {
          inPointSeconds = speechSegs[0].start;
          outPointSeconds = speechSegs[speechSegs.length - 1].end;
          updateTimecodeUI();
        }
        detectSummary.style.display = 'block';
        detectSummary.textContent = `Auto-Detected ${res.segments.length} segments (${speechSegs.length} speech). Suggested In: ${inPointSeconds.toFixed(2)}s, Out: ${outPointSeconds.toFixed(2)}s.`;
        showResultBanner('Speech boundary detection complete!');
      } else {
        alert(res.detail || 'Detection failed');
      }
    } catch (err) {
      alert(`Detection error: ${err.message}`);
    } finally {
      runDetectBtn.disabled = false;
      runDetectBtn.textContent = 'Auto-Detect Speech Boundaries (detect.py)';
    }
  });

  // STEP 1: Cut Execution
  runCutBtn.addEventListener('click', async () => {
    runCutBtn.disabled = true;
    runCutBtn.textContent = 'Trimming video...';

    try {
      const resp = await fetch('/api/pipeline/cut', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_path: 'data/1/raw/sample-3.mp4',
          start_seconds: inPointSeconds,
          end_seconds: outPointSeconds,
          force_reencode: forceReencodeCheckbox.checked,
          output_filename: 'sample-cut.mp4'
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Cut successful! Strategy: ${res.strategy_used}. Loaded ${res.filename} in player.`);
      } else {
        alert(res.detail || 'Cut failed');
      }
    } catch (err) {
      alert(`Cut error: ${err.message}`);
    } finally {
      runCutBtn.disabled = false;
      runCutBtn.textContent = 'Execute Video Cut (cut.py)';
    }
  });

  // STEP 2: Loudness Execution
  runLoudnessBtn.addEventListener('click', async () => {
    runLoudnessBtn.disabled = true;
    runLoudnessBtn.textContent = 'Normalizing audio with EBU R128...';

    try {
      const resp = await fetch('/api/pipeline/loudness', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_path: 'data/1/raw/sample-3.mp4',
          target_lufs: parseFloat(targetLufsInput.value) || -16.0,
          output_filename: 'sample-normalized.mp4'
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Loudness normalization complete at ${res.target_lufs} LUFS! Loaded ${res.filename} in player.`);
      } else {
        alert(res.detail || 'Normalization failed');
      }
    } catch (err) {
      alert(`Normalization error: ${err.message}`);
    } finally {
      runLoudnessBtn.disabled = false;
      runLoudnessBtn.textContent = 'Execute Loudness Normalization (loudness.py)';
    }
  });

  // STEP 3: Intro Execution
  runIntroBtn.addEventListener('click', async () => {
    runIntroBtn.disabled = true;
    runIntroBtn.textContent = 'Rendering intro slate...';

    try {
      const speakersList = introSpeakersInput.value.split(',').map(s => s.trim()).filter(Boolean);
      const resp = await fetch('/api/pipeline/intro', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: introTitleInput.value,
          speakers: speakersList,
          event_name: introEventInput.value,
          room_date: introRoomInput.value,
          duration_seconds: 4.0,
          output_filename: 'sample-intro.mp4'
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Rendered 1080p Intro Slate (4.0s)! Loaded ${res.filename} in player.`);
      } else {
        alert(res.detail || 'Intro generation failed');
      }
    } catch (err) {
      alert(`Intro error: ${err.message}`);
    } finally {
      runIntroBtn.disabled = false;
      runIntroBtn.textContent = 'Generate & Preview Intro Slate (4.0s)';
    }
  });

  // STEP 3: Outro Execution
  runOutroBtn.addEventListener('click', async () => {
    runOutroBtn.disabled = true;
    runOutroBtn.textContent = 'Rendering outro slate...';

    try {
      const resp = await fetch('/api/pipeline/outro', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_name: introEventInput.value,
          thank_you_text: outroThankYouInput.value,
          website_or_links: outroLinksInput.value,
          duration_seconds: 3.5,
          output_filename: 'sample-outro.mp4'
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Rendered 1080p Outro Slate (3.5s)! Loaded ${res.filename} in player.`);
      } else {
        alert(res.detail || 'Outro generation failed');
      }
    } catch (err) {
      alert(`Outro error: ${err.message}`);
    } finally {
      runOutroBtn.disabled = false;
      runOutroBtn.textContent = 'Generate & Preview Outro Slate (3.5s)';
    }
  });

  // STEP 4: Preview Execution
  runPreviewBtn.addEventListener('click', async () => {
    runPreviewBtn.disabled = true;
    runPreviewBtn.textContent = 'Generating preview proxy...';

    try {
      const preset = previewPresetSelect.value;
      const resp = await fetch('/api/pipeline/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_path: 'data/1/raw/sample-3.mp4',
          preset_name: preset,
          output_filename: `sample-preview-${preset}.mp4`
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Generated low-res preview with preset "${res.preset}"! Loaded in player.`);
      } else {
        alert(res.detail || 'Preview generation failed');
      }
    } catch (err) {
      alert(`Preview error: ${err.message}`);
    } finally {
      runPreviewBtn.disabled = false;
      runPreviewBtn.textContent = 'Generate Low-Res Preview Proxy (preview.py)';
    }
  });

  // STEP 5: Transcode Execution
  runTranscodeBtn.addEventListener('click', async () => {
    runTranscodeBtn.disabled = true;
    runTranscodeBtn.textContent = 'Transcoding master video...';
    openJobMonitor('Transcode Master', 'Rendering H.264 High-Profile Video');

    try {
      const preset = transcodePresetSelect.value;
      const resp = await fetch('/api/pipeline/transcode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input_path: 'data/1/raw/sample-3.mp4',
          preset_name: preset,
          output_filename: `sample-transcoded-${preset}.mp4`
        })
      });

      const res = await resp.json();
      if (resp.ok) {
        loadVideo(res.output_path);
        showResultBanner(`Transcoded final master quality video (${res.preset})! Loaded in player.`);
      } else {
        alert(res.detail || 'Transcode failed');
      }
    } catch (err) {
      alert(`Transcode error: ${err.message}`);
    } finally {
      runTranscodeBtn.disabled = false;
      runTranscodeBtn.textContent = 'Execute Final Master Transcode (transcode.py)';
    }
  });

  // Video Loader Helper
  function loadVideo(src) {
    currentVideoSrc = src + '?t=' + Date.now();
    previewVideo.src = currentVideoSrc;
    previewVideo.load();
    previewVideo.play().catch(() => {});
  }

  function showResultBanner(text) {
    stepResultText.textContent = text;
    stepResultBanner.style.display = 'block';
  }

  // API Calls & Dashboard
  async function fetchTalks() {
    talksTableBody.innerHTML = `
      <tr class="empty-row"><td colspan="6"><div class="empty-state"><p>Loading talks from VEditor API...</p></div></td></tr>
    `;

    try {
      const response = await fetch('/ops/talks', {
        headers: {
          'X-API-Key': currentApiKey,
          'Accept': 'application/json',
        }
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data = await response.json();
      allTalks = (Array.isArray(data) && data.length > 0) ? data : DEMO_TALKS;
      updateStats();
      renderTalks();
    } catch (err) {
      console.warn('[VEditor UI] API request failed (falling back to demo mode):', err.message);
      allTalks = DEMO_TALKS;
      updateStats();
      renderTalks(true);
    }
  }

  function updateStats() {
    statTotalTalks.textContent = allTalks.length;
    statPending.textContent = allTalks.filter(t => t.state === 'pending_approval' || t.state === 'waiting_for_files').length;
    statProcessing.textContent = allTalks.filter(t => t.state === 'cutting' || t.state === 'preview').length;
    statDone.textContent = allTalks.filter(t => t.state === 'done').length;
  }

  function renderTalks(isDemoFallback = false) {
    const query = searchTalksInput.value.toLowerCase().trim();
    const filtered = allTalks.filter(t => {
      const matchesFilter = currentFilter === 'all' || t.state === currentFilter;
      const matchesQuery = !query || 
        (t.title && t.title.toLowerCase().includes(query)) ||
        (t.speakers && JSON.stringify(t.speakers).toLowerCase().includes(query)) ||
        (t.event_id && String(t.event_id).includes(query));
      return matchesFilter && matchesQuery;
    });

    if (filtered.length === 0) {
      talksTableBody.innerHTML = `
        <tr class="empty-row"><td colspan="6"><div class="empty-state"><p>No talks found matching current criteria.</p></div></td></tr>
      `;
      return;
    }

    const demoNotice = isDemoFallback ? `
      <tr>
        <td colspan="6" style="background: rgba(229, 62, 62, 0.08); border-bottom: 1px solid rgba(229, 62, 62, 0.2); padding: 0.5rem 1rem; font-size: 0.78rem; color: #fca5a5;">
          <strong>Interactive Pipeline Studio:</strong> Connected in local standalone mode with live execution. Click "Open Pipeline Studio" to test all pipeline scripts step by step.
        </td>
      </tr>
    ` : '';

    talksTableBody.innerHTML = demoNotice + filtered.map(talk => {
      const speakersText = Array.isArray(talk.speakers) ? talk.speakers.join(', ') : (talk.speakers || 'Unassigned');
      const badgeClass = getBadgeClass(talk.state);

      return `
        <tr>
          <td style="font-family: var(--font-mono); color: var(--text-muted);">#${talk.id}</td>
          <td>
            <div class="talk-title-cell">${escapeHtml(talk.title || 'Untitled Talk')}</div>
            <div class="talk-speaker-cell">${escapeHtml(speakersText)}</div>
          </td>
          <td>
            <span style="font-weight: 600;">Event #${talk.event_id}</span>
            <div style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(talk.room_date || 'Main Track')}</div>
          </td>
          <td>
            <span class="badge ${badgeClass}">${escapeHtml(talk.state || 'unknown')}</span>
          </td>
          <td style="font-family: var(--font-mono); font-size: 0.78rem; color: var(--text-secondary);">
            ${talk.start_seconds !== undefined ? `${talk.start_seconds.toFixed(1)}s - ${talk.end_seconds ? talk.end_seconds.toFixed(1) : '?'}s` : 'Full Duration'}
          </td>
          <td>
            <div style="display: flex; gap: 0.4rem;">
              <button class="btn btn-primary btn-sm review-talk-btn" data-talk-id="${talk.id}">Open Pipeline Studio</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

    document.querySelectorAll('.review-talk-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const talkId = parseInt(btn.dataset.talkId, 10);
        openCutterModal(talkId);
      });
    });
  }

  function openCutterModal(talkId) {
    activeTalk = allTalks.find(t => t.id === talkId);
    if (!activeTalk) return;

    modalTalkTitle.textContent = activeTalk.title || `Talk #${activeTalk.id}`;
    modalTalkEventBadge.textContent = `Event #${activeTalk.event_id}`;

    inPointSeconds = activeTalk.start_seconds || 2.0;
    outPointSeconds = activeTalk.end_seconds || 12.0;

    introTitleInput.value = activeTalk.title || 'Open Source Video Editing & Transcoding Pipeline';
    introSpeakersInput.value = Array.isArray(activeTalk.speakers) ? activeTalk.speakers.join(', ') : (activeTalk.speakers || 'Saalim, ViRUS-0-0');

    loadVideo('/data/1/raw/sample-3.mp4');
    updateTimecodeUI();
    cutterModal.classList.add('active');
  }

  function updateTimecodeUI() {
    tcStart.textContent = `${inPointSeconds.toFixed(2)}s`;
    tcEnd.textContent = `${outPointSeconds.toFixed(2)}s`;
    const dur = Math.max(0, outPointSeconds - inPointSeconds);
    tcDuration.textContent = `${dur.toFixed(2)}s`;
  }

  function openMultiStageJobMonitor(taskTitle) {
    jobMonitorId.textContent = taskTitle;
    jobMonitorStatus.textContent = 'running';
    jobMonitorStatus.className = 'badge badge-cutting';
    jobProgressPct.textContent = '0% • 1/5 Trimming talk with cut.py';
    jobProgressBar.style.width = '10%';
    jobProgressModal.classList.add('active');

    const stages = [
      { pct: 20, text: '20% • 1/5 Trimming talk with cut.py' },
      { pct: 40, text: '40% • 2/5 Normalizing audio with loudness.py' },
      { pct: 60, text: '60% • 3/5 Rendering Intro & Outro with intro.py' },
      { pct: 85, text: '85% • 4/5 Stitching & Transcoding Master with transcode.py' },
      { pct: 100, text: '100% • 5/5 Pipeline Complete!' },
    ];

    let currentStage = 0;
    if (jobPollingInterval) clearInterval(jobPollingInterval);

    jobPollingInterval = setInterval(() => {
      if (currentStage < stages.length) {
        jobProgressBar.style.width = `${stages[currentStage].pct}%`;
        jobProgressPct.textContent = stages[currentStage].text;
        currentStage++;
      } else {
        clearInterval(jobPollingInterval);
        jobMonitorStatus.textContent = 'finished';
        jobMonitorStatus.className = 'badge badge-done';
        setTimeout(() => {
          jobProgressModal.classList.remove('active');
        }, 800);
      }
    }, 450);
  }

  function openJobMonitor(taskTitle, stageDescription) {
    jobMonitorId.textContent = taskTitle;
    jobMonitorStatus.textContent = 'running';
    jobProgressPct.textContent = '0% Completed';
    jobProgressBar.style.width = '0%';
    jobProgressModal.classList.add('active');

    let currentPct = 0;
    if (jobPollingInterval) clearInterval(jobPollingInterval);

    jobPollingInterval = setInterval(() => {
      currentPct += 25;
      if (currentPct > 100) currentPct = 100;
      jobProgressBar.style.width = `${currentPct}%`;
      jobProgressPct.textContent = `${currentPct}% Completed`;

      if (currentPct >= 100) {
        clearInterval(jobPollingInterval);
        jobMonitorStatus.textContent = 'finished';
        jobMonitorStatus.className = 'badge badge-done';
        setTimeout(() => {
          jobProgressModal.classList.remove('active');
        }, 800);
      }
    }, 300);
  }

  function getBadgeClass(state) {
    switch (state) {
      case 'pending_approval':
      case 'waiting_for_files':
        return 'badge-pending';
      case 'cutting':
        return 'badge-cutting';
      case 'preview':
        return 'badge-preview';
      case 'done':
        return 'badge-done';
      case 'failed':
      case 'rejected':
        return 'badge-failed';
      default:
        return 'badge-pending';
    }
  }

  function formatTimecode(seconds) {
    if (isNaN(seconds)) return '00:00.00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    const ms = Math.floor((seconds % 1) * 100);
    return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(ms).padStart(2, '0')}`;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Initial Load
  fetchTalks();
});
