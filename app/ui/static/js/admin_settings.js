document.addEventListener("DOMContentLoaded", () => {
  const PRESET_VALUES = [60, 180, 300, 600, 900, 1800];
  const slider = document.getElementById("slider-detect_duration_tolerance_seconds");
  const hiddenInput = document.getElementById("input-detect_duration_tolerance_seconds");
  const readout = document.getElementById("readout-detect_duration_tolerance_seconds");
  const badge = document.getElementById("badge-detect_duration_tolerance_seconds");
  const presetPills = document.querySelectorAll(".preset-pill");

  function syncTolerance(index, updateInput = true) {
    const idx = Math.max(0, Math.min(PRESET_VALUES.length - 1, index));
    const sec = PRESET_VALUES[idx];
    if (slider) slider.value = idx;
    if (updateInput && hiddenInput) hiddenInput.value = sec.toFixed(1);
    const effectiveSec = updateInput ? sec : (parseFloat(hiddenInput?.value) || sec);
    if (readout) readout.textContent = `${Math.round(effectiveSec / 60)} mins${effectiveSec === 300 ? " (Default)" : ""}`;
    if (badge) {
      const isDef = effectiveSec === 300;
      badge.className = `setting-badge ${isDef ? "badge-default" : "badge-custom"}`;
      badge.textContent = isDef ? "System Default" : "Custom Override";
    }
    const isExact = PRESET_VALUES.includes(effectiveSec);
    presetPills.forEach((p) => p.classList.toggle("active", isExact && Number(p.dataset.index) === idx));
  }

  if (slider && hiddenInput) {
    const initialSec = parseFloat(hiddenInput.value || "300");
    const initialIdx = PRESET_VALUES.reduce((closest, val, i, arr) =>
      Math.abs(val - initialSec) < Math.abs(arr[closest] - initialSec) ? i : closest, 2
    );
    syncTolerance(initialIdx, false);
    slider.addEventListener("input", (e) => syncTolerance(parseInt(e.target.value, 10)));
  }

  presetPills.forEach((pill) => {
    pill.addEventListener("click", () => syncTolerance(parseInt(pill.dataset.index, 10)));
  });

  const defaults = {
    loudness_target_lufs: (v) => parseFloat(v) === -16.0,
    default_preview_preset: (v) => v === "small_video",
    default_transcode_preset: (v) => v === "1080p_default",
  };
  Object.entries(defaults).forEach(([key, checkDefault]) => {
    const sel = document.getElementById(`select-${key}`);
    const b = document.getElementById(`badge-${key}`);
    if (sel && b) {
      sel.addEventListener("change", () => {
        const isDef = checkDefault(sel.value);
        b.className = `setting-badge ${isDef ? "badge-default" : "badge-custom"}`;
        b.textContent = isDef ? "System Default" : "Custom Override";
      });
    }
  });

  const modal = document.getElementById("reset-modal");
  document.getElementById("btn-open-reset-modal")?.addEventListener("click", () => Modal.open(modal));
  document.getElementById("btn-cancel-modal")?.addEventListener("click", () => Modal.close(modal));

  setTimeout(() => {
    document.querySelectorAll(".admin-alert").forEach((a) => {
      a.classList.add("alert-fade-out");
      setTimeout(() => a.remove(), 400);
    });
  }, 5000);
});
