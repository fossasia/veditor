document.addEventListener("DOMContentLoaded", () => {
  const PRESET_VALUES = [60, 180, 300, 600, 900, 1800];
  const slider = document.getElementById("slider-detect_duration_tolerance_seconds");
  const hiddenInput = document.getElementById("input-detect_duration_tolerance_seconds");
  const readout = document.getElementById("readout-detect_duration_tolerance_seconds");
  const badge = document.getElementById("badge-detect_duration_tolerance_seconds");
  const presetPills = document.querySelectorAll(".preset-pill");

  function syncTolerance(index) {
    const idx = Math.max(0, Math.min(PRESET_VALUES.length - 1, index));
    const sec = PRESET_VALUES[idx];
    if (slider) slider.value = idx;
    if (hiddenInput) hiddenInput.value = sec.toFixed(1);
    if (readout) readout.textContent = `${Math.round(sec / 60)} mins${sec === 300 ? " (Default)" : ""}`;
    if (badge) {
      const isDef = sec === 300;
      badge.className = `setting-badge ${isDef ? "badge-default" : "badge-custom"}`;
      badge.textContent = isDef ? "System Default" : "Custom Override";
    }
    presetPills.forEach((p) => p.classList.toggle("active", Number(p.dataset.index) === idx));
  }

  if (slider && hiddenInput) {
    const initialSec = parseFloat(hiddenInput.value || "300");
    const initialIdx = PRESET_VALUES.reduce((closest, val, i, arr) =>
      Math.abs(val - initialSec) < Math.abs(arr[closest] - initialSec) ? i : closest, 2
    );
    syncTolerance(initialIdx);
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
  document.getElementById("btn-open-reset-modal")?.addEventListener("click", () => modal?.showModal());
  document.getElementById("btn-cancel-modal")?.addEventListener("click", () => modal?.close());
  modal?.addEventListener("click", (e) => {
    if (e.target === modal) modal.close();
  });

  setTimeout(() => {
    document.querySelectorAll(".admin-alert").forEach((a) => {
      a.classList.add("alert-fade-out");
      setTimeout(() => a.remove(), 400);
    });
  }, 5000);
});
