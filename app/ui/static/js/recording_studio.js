/**
 * Recording Studio — Centralised Video Review & Bulk Handoff
 */
document.addEventListener('DOMContentLoaded', function () {
  'use strict';

  var form = document.getElementById('form-bulk-approve');
  var btnApprove = document.getElementById('btn-bulk-approve');
  var btnReject = document.getElementById('btn-reject-recording');
  var statusBanner = document.getElementById('rec-status-banner');

  if (!form || !btnApprove) {
    return;
  }

  var recordingId = form.dataset.recordingId;
  var videoPlayer = document.getElementById('rec-video-player');

  function seekVideoToPointer(seconds) {
    if (!videoPlayer || isNaN(seconds)) return;
    try {
      videoPlayer.currentTime = seconds;
    } catch (e) {
      // If metadata not ready yet, wait for loadedmetadata
      videoPlayer.addEventListener('loadedmetadata', function () {
        videoPlayer.currentTime = seconds;
      }, { once: true });
    }
  }

  // Seek video player to initial talk start pointer if available
  if (videoPlayer) {
    var initialCard = document.querySelector('.rec-talk-card.is-recommended') || document.querySelector('.rec-talk-card');
    if (initialCard && initialCard.dataset.pointerSeconds) {
      var initialPtr = parseFloat(initialCard.dataset.pointerSeconds);
      if (initialPtr > 0) {
        seekVideoToPointer(initialPtr);
      }
    }
  }

  // Handle clicking talk cards and seek buttons to jump video pointer
  document.querySelectorAll('.rec-talk-card').forEach(function (card) {
    card.addEventListener('click', function (e) {
      if (e.target && e.target.type === 'checkbox') {
        return; // Checkbox click handled natively
      }
      var ptr = parseFloat(card.dataset.pointerSeconds || '0');
      seekVideoToPointer(ptr);
    });
  });

  document.querySelectorAll('.btn-seek-pointer').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var ptr = parseFloat(btn.dataset.seek || '0');
      seekVideoToPointer(ptr);
    });
  });

  // Bumper Controls Setup (Intro & Outro)
  ['intro', 'outro'].forEach(function (type) {
    var toggle = document.getElementById('check-include-' + type);
    var wrap = document.getElementById(type + '-options-wrap');
    if (toggle && wrap) {
      toggle.addEventListener('change', function () {
        wrap.classList.toggle('is-hidden', !toggle.checked);
      });
    }

    var radios = document.querySelectorAll('input[name="' + type + '_source"]');
    var customWrap = document.getElementById(type + '-custom-wrap');
    var fileInput = document.getElementById('custom-' + type + '-file');
    var pathInput = document.getElementById('custom-' + type + '-path');
    var dropzone = document.getElementById(type + '-dropzone');
    var fileName = document.getElementById(type + '-file-name');

    radios.forEach(function (r) {
      r.addEventListener('change', function () {
        radios.forEach(function (rad) {
          rad.closest('.bumper-segment')?.classList.toggle('is-active', rad.checked);
        });
        if (customWrap) {
          customWrap.classList.toggle('is-visible', r.value === 'custom' && r.checked);
        }
      });
    });

    async function handleBumperUpload(file) {
      if (!file) return;
      if (fileName) {
        fileName.textContent = file.name + ' (Uploading...)';
      }
      try {
        var fd = new FormData();
        fd.append('file', file);
        fd.append('kind', type);
        var fetchFn = window.authFetch || fetch;
        var res = await fetchFn('/studio/recordings/' + recordingId + '/bumpers/upload', {
          method: 'POST',
          body: fd
        });
        if (!res.ok) {
          var err = await res.json().catch(function () { return {}; });
          throw new Error(err.detail || 'Upload failed');
        }
        var data = await res.json();
        if (pathInput) pathInput.value = data.path;
        if (fileName) fileName.textContent = file.name + ' ✓';
      } catch (err) {
        if (fileName) fileName.textContent = 'Upload failed: ' + err.message;
      }
    }

    if (fileInput) {
      fileInput.addEventListener('change', function () {
        if (fileInput.files && fileInput.files[0]) {
          handleBumperUpload(fileInput.files[0]);
        }
      });
    }

    if (dropzone) {
      ['dragenter', 'dragover'].forEach(function (evt) {
        dropzone.addEventListener(evt, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.add('drag-over');
        });
      });
      ['dragleave', 'drop'].forEach(function (evt) {
        dropzone.addEventListener(evt, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.remove('drag-over');
        });
      });
      dropzone.addEventListener('drop', function (e) {
        var dt = e.dataTransfer;
        if (dt && dt.files && dt.files.length) {
          handleBumperUpload(dt.files[0]);
        }
      });
    }
  });

  // Handoff Card & Action Button Text Synchronization
  var chkHandoff = document.getElementById('chk-handoff');
  var handoffCard = document.getElementById('rec-handoff-card');

  function updateHandoffState() {
    var isHandoff = chkHandoff ? chkHandoff.checked : true;
    if (handoffCard) {
      handoffCard.classList.toggle('is-active', isHandoff);
    }
    if (btnApprove) {
      btnApprove.textContent = isHandoff ? 'Approve & Handoff Selected Talks' : 'Approve & Attach Selected Talks';
    }
  }

  if (chkHandoff) {
    chkHandoff.addEventListener('change', updateHandoffState);
  }
  updateHandoffState();

  function setStatus(message, isError) {
    if (!statusBanner) return;
    statusBanner.textContent = message;
    statusBanner.className = 'rec-status-banner ' + (isError ? 'is-error' : 'is-success');
    statusBanner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function clearStatus() {
    if (!statusBanner) return;
    statusBanner.textContent = '';
    statusBanner.className = 'rec-status-banner';
  }

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    clearStatus();

    var checkedBoxes = form.querySelectorAll('input[name="talk_ids"]:checked');
    var talkIds = [];
    checkedBoxes.forEach(function (cb) {
      talkIds.push(parseInt(cb.value, 10));
    });

    if (talkIds.length === 0) {
      setStatus('Please select at least one talk to approve.', true);
      return;
    }

    var includeIntro = !!document.getElementById('check-include-intro')?.checked;
    var includeOutro = !!document.getElementById('check-include-outro')?.checked;
    var introSource = document.querySelector('input[name="intro_source"]:checked')?.value || 'generated';
    var outroSource = document.querySelector('input[name="outro_source"]:checked')?.value || 'generated';
    var customIntroPath = document.getElementById('custom-intro-path')?.value || null;
    var customOutroPath = document.getElementById('custom-outro-path')?.value || null;
    var handoff = !!document.getElementById('chk-handoff')?.checked;

    var payload = {
      talk_ids: talkIds,
      include_intro: includeIntro,
      include_outro: includeOutro,
      intro_source: introSource,
      outro_source: outroSource,
      custom_intro_path: (includeIntro && introSource === 'custom') ? customIntroPath : null,
      custom_outro_path: (includeOutro && outroSource === 'custom') ? customOutroPath : null,
      handoff: handoff
    };

    btnApprove.disabled = true;
    btnApprove.textContent = 'Processing...';

    try {
      var fetchFn = window.authFetch || fetch;
      var response = await fetchFn('/studio/recordings/' + recordingId + '/bulk-approve', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      });

      var data = await response.json().catch(function () { return {}; });

      if (!response.ok) {
        throw new Error(data.detail || ('Server returned HTTP ' + response.status));
      }

      var msg = 'Successfully approved and attached video to ' +
        (data.approved_count || talkIds.length) + ' talk(s)!';
      if (handoff) {
        msg += ' Speaker handoff review emails dispatched.';
      }

      setStatus(msg, false);
      btnApprove.textContent = 'Approved!';

      setTimeout(function () {
        window.location.href = '/studio';
      }, 1800);
    } catch (err) {
      setStatus('Failed to approve talks: ' + err.message, true);
      btnApprove.disabled = false;
      btnApprove.textContent = 'Approve & Handoff Selected';
    }
  });

  if (btnReject) {
    btnReject.addEventListener('click', async function () {
      if (!confirm('Are you sure you want to reject this recording?')) {
        return;
      }
      clearStatus();
      btnReject.disabled = true;
      btnReject.textContent = 'Rejecting...';

      try {
        var fetchFn = window.authFetch || fetch;
        var response = await fetchFn('/studio/recordings/' + recordingId + '/reject', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          }
        });

        if (!response.ok) {
          var data = await response.json().catch(function () { return {}; });
          throw new Error(data.detail || ('Server returned HTTP ' + response.status));
        }

        setStatus('Recording marked as rejected.', false);
        setTimeout(function () {
          window.location.href = '/studio';
        }, 1200);
      } catch (err) {
        setStatus('Failed to reject recording: ' + err.message, true);
        btnReject.disabled = false;
        btnReject.textContent = 'Reject Recording';
      }
    });
  }
});
