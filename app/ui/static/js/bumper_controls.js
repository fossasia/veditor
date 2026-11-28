/**
 * Shared Intro/Outro Bumper Studio Controls
 */
window.initBumperControls = function (uploadUrl, options) {
  'use strict';
  const opts = options || {};
  let activeUploads = 0;

  function notifyUploadState() {
    if (typeof opts.onUploadStateChange === 'function') {
      opts.onUploadStateChange(activeUploads > 0);
    }
  }

  ['intro', 'outro'].forEach(function (type) {
    const toggle = document.getElementById('check-include-' + type);
    const wrap = document.getElementById(type + '-options-wrap');
    if (toggle && wrap) {
      toggle.addEventListener('change', function () {
        wrap.classList.toggle('is-hidden', !toggle.checked);
      });
    }

    const radios = document.querySelectorAll('input[name="' + type + '_source"]');
    const customWrap = document.getElementById(type + '-custom-wrap');
    const fileInput = document.getElementById('custom-' + type + '-file');
    const pathInput = document.getElementById('custom-' + type + '-path');
    const dropzone = document.getElementById(type + '-dropzone');
    const fileName = document.getElementById(type + '-file-name');
    let currentUploadGen = 0;

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

    async function handleSelectedFile(selected) {
      if (!selected) return;
      const uploadGen = ++currentUploadGen;
      activeUploads += 1;
      notifyUploadState();
      if (fileName) {
        fileName.textContent = selected.name + ' (Uploading...)';
        fileName.classList.remove('is-ready');
      }
      if (pathInput) pathInput.value = '';
      try {
        const fd = new FormData();
        fd.append('file', selected);
        fd.append('kind', type);
        const res = await (window.authFetch || fetch)(uploadUrl, {
          method: 'POST',
          body: fd,
        });
        if (uploadGen !== currentUploadGen) return;
        if (!res.ok) {
          const err = await res.json().catch(function () { return {}; });
          throw new Error(err.detail || 'Upload failed');
        }
        const data = await res.json();
        if (uploadGen !== currentUploadGen) return;
        if (data.path) {
          if (pathInput) pathInput.value = data.path;
          if (fileName) {
            fileName.textContent = selected.name + ' ✓';
            fileName.classList.add('is-ready');
          }
        }
      } catch (err) {
        if (uploadGen === currentUploadGen && fileName) {
          fileName.textContent = 'Upload failed: ' + err.message;
        }
      } finally {
        activeUploads = Math.max(0, activeUploads - 1);
        notifyUploadState();
      }
    }

    if (fileInput) {
      fileInput.addEventListener('change', function () {
        const selected = fileInput.files && fileInput.files[0];
        if (selected) handleSelectedFile(selected);
      });
    }

    if (dropzone) {
      ['dragenter', 'dragover'].forEach(function (name) {
        dropzone.addEventListener(name, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.add('drag-over');
        });
      });
      ['dragleave', 'drop'].forEach(function (name) {
        dropzone.addEventListener(name, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.remove('drag-over');
        });
      });
      dropzone.addEventListener('drop', function (e) {
        const dt = e.dataTransfer;
        const dropped = dt && dt.files && dt.files[0];
        if (dropped) {
          if (fileInput) {
            try {
              fileInput.files = dt.files;
            } catch (_) {}
          }
          handleSelectedFile(dropped);
        }
      });
    }

    if (pathInput && pathInput.value && pathInput.value.trim() !== '' && fileName) {
      fileName.classList.add('is-ready');
    }
  });
};
