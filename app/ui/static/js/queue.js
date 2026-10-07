/**
 * Admin Priority Queue (Music Player Queue Interaction & Live Polling)
 */

(function () {
  "use strict";

  let isDragging = false;
  let draggedCard = null;
  let draggedTalkId = null;
  let draggedSourceQueue = null;
  let pollInterval = null;

  const POLLING_INTERVAL_MS = 3000;

  function formatTime(seconds) {
    if (seconds == null || isNaN(seconds)) return "--:--";
    const totalSec = Math.max(0, Math.floor(seconds));
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) {
      return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
    }
    return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }

  function formatStageName(kind) {
    if (!kind) return "Pending";
    const names = {
      ingest: "Ingesting Raw Video",
      detect: "Analyzing Container & Codecs",
      cut: "Cutting to Bounds",
      intro: "Generating Intro Title Card",
      outro: "Generating Outro Card",
      concat: "Concatenating Segments",
      preview: "Rendering Web Preview",
      loudness: "Normalizing Loudness (EBU R128)",
      transcode: "High-Definition Transcoding",
      publish: "Publishing Final Deliverable",
      waiting_for_files: "Waiting for Recording Files",
      pending_approval: "Pending Initial Approval",
      pending_bounds: "Pending Boundary Cuts",
      pending_intro_outro: "Pending Title Cards",
      needs_work: "Review Requested Changes",
    };
    return names[kind] || kind.charAt(0).toUpperCase() + kind.slice(1);
  }

  function createCardElement(talk, queueType) {
    const card = document.createElement("div");
    card.className = "talk-card";
    card.setAttribute("draggable", "true");
    card.dataset.talkId = talk.id;
    card.dataset.queueType = queueType;

    const np = talk.now_playing || {};
    const statusClass = `status-${np.status || "pending"}`;
    const pct = Math.min(100, Math.max(0, np.progress_pct || 0)).toFixed(1);
    const isPriority = queueType === "priority";

    let rightActionHtml = "";
    if (isPriority) {
      rightActionHtml = `
        <div class="card-actions-right">
          <span class="rank-badge">#${talk.priority_rank || ""}</span>
          <button type="button" class="btn-deprioritize" data-action="deprioritize" data-talk-id="${talk.id}" title="Remove from Priority" aria-label="Remove from Priority">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      `;
    } else {
      rightActionHtml = `
        <div class="card-actions-right">
          <button type="button" class="btn-prioritize" data-action="prioritize" data-talk-id="${talk.id}" title="Move to Priority Queue" aria-label="Move to Priority Queue">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
            </svg>
          </button>
        </div>
      `;
    }

    let upcomingHtml = "";
    if (talk.upcoming_stages && talk.upcoming_stages.length > 0) {
      const pills = talk.upcoming_stages
        .map(
          (st) =>
            `<span class="stage-pill">${st.charAt(0).toUpperCase() + st.slice(1)}</span>`
        )
        .join('<span class="stage-arrow">→</span>');
      upcomingHtml = `
        <div class="upcoming-stages-row">
          <span>Up Next:</span>
          ${pills}
        </div>
      `;
    }

    card.innerHTML = `
      <div class="card-header-line">
        <div class="card-title-meta">
          <h3 class="talk-title" title="${escapeHtml(talk.title)}">${escapeHtml(talk.title)}</h3>
          <div class="talk-meta">
            <span>${escapeHtml(talk.event_name || "")}</span>
            ${talk.room ? `<span>•</span><span>${escapeHtml(talk.room)}</span>` : ""}
          </div>
        </div>
        ${rightActionHtml}
      </div>

      <div class="now-playing-box">
        <div class="now-playing-top">
          <div class="now-playing-label">
            <span class="status-badge ${statusClass}">${escapeHtml(np.status || "pending")}</span>
            <span>${escapeHtml(formatStageName(np.kind))}</span>
          </div>
          <span class="now-playing-pct">${pct}%</span>
        </div>
        <div class="track-scrubber">
          <div class="track-progress ${isPriority ? "is-priority" : ""}" style="width: ${pct}%;"></div>
        </div>
        <div class="now-playing-bottom">
          <span>Elapsed: ${formatTime(np.elapsed_time)}</span>
          <span>Remaining: ${formatTime(np.estimated_remaining)}</span>
        </div>
      </div>

      ${upcomingHtml}
    `;

    attachCardDragListeners(card);
    return card;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function attachCardDragListeners(card) {
    card.addEventListener("dragstart", (e) => {
      isDragging = true;
      draggedCard = card;
      draggedTalkId = parseInt(card.dataset.talkId, 10);
      draggedSourceQueue = card.dataset.queueType;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.talkId);
    });

    card.addEventListener("dragend", () => {
      isDragging = false;
      if (draggedCard) {
        draggedCard.classList.remove("dragging");
      }
      clearDropIndicators();
      draggedCard = null;
      draggedTalkId = null;
      draggedSourceQueue = null;
    });

    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (!draggedCard || draggedCard === card) return;

      const rect = card.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;

      clearDropIndicators();
      if (e.clientY < midY) {
        card.classList.add("drop-before");
      } else {
        card.classList.add("drop-after");
      }
    });

    card.addEventListener("dragleave", () => {
      card.classList.remove("drop-before", "drop-after");
    });
  }

  function clearDropIndicators() {
    document.querySelectorAll(".talk-card").forEach((c) => {
      c.classList.remove("drop-before", "drop-after");
    });
    document.querySelectorAll(".queue-panel").forEach((p) => {
      p.classList.remove("drag-over");
    });
  }

  function setupDropZones() {
    const zones = ["light", "heavy", "priority"];

    zones.forEach((zoneType) => {
      const listEl = document.getElementById(`list-${zoneType}`);
      const panelEl = document.getElementById(`panel-${zoneType}`);
      if (!listEl || !panelEl) return;

      panelEl.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        panelEl.classList.add("drag-over");
      });

      panelEl.addEventListener("dragleave", (e) => {
        if (!panelEl.contains(e.relatedTarget)) {
          panelEl.classList.remove("drag-over");
        }
      });

      panelEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        panelEl.classList.remove("drag-over");

        if (!draggedTalkId) return;

        const targetCard = e.target.closest(".talk-card");
        const talkId = draggedTalkId;
        const sourceQueue = draggedSourceQueue;

        if (zoneType === "priority") {
          if (sourceQueue === "priority") {
            // Reordering inside Priority
            handlePriorityReorder(listEl, draggedCard, targetCard, e);
          } else {
            // Dragged from Light/Heavy to Priority
            let targetRank = null;
            if (targetCard && targetCard !== draggedCard) {
              const rect = targetCard.getBoundingClientRect();
              const midY = rect.top + rect.height / 2;
              const cards = Array.from(listEl.querySelectorAll(".talk-card"));
              const targetIdx = cards.indexOf(targetCard);
              targetRank = e.clientY < midY ? targetIdx + 1 : targetIdx + 2;
            }
            await prioritizeTalk(talkId, targetRank);
          }
        } else if (sourceQueue === "priority") {
          // Dragged from Priority back to regular queue
          await deprioritizeTalk(talkId);
        }

        clearDropIndicators();
        await fetchQueueData();
      });
    });
  }

  async function handlePriorityReorder(listEl, draggedEl, targetCard, dropEvent) {
    if (!targetCard || targetCard === draggedEl) return;

    const rect = targetCard.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (dropEvent.clientY < midY) {
      listEl.insertBefore(draggedEl, targetCard);
    } else {
      listEl.insertBefore(draggedEl, targetCard.nextSibling);
    }

    const cards = Array.from(listEl.querySelectorAll(".talk-card"));
    const orderedTalkIds = cards.map((c) => parseInt(c.dataset.talkId, 10));

    try {
      await fetch("/admin/api/queue/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ordered_talk_ids: orderedTalkIds }),
      });
    } catch (err) {
      console.error("Failed to reorder priority queue:", err);
    }
  }

  async function prioritizeTalk(talkId, targetRank) {
    try {
      await fetch(`/admin/api/queue/talks/${talkId}/prioritize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_rank: targetRank }),
      });
    } catch (err) {
      console.error("Failed to prioritize talk:", err);
    }
  }

  async function deprioritizeTalk(talkId) {
    try {
      await fetch(`/admin/api/queue/talks/${talkId}/deprioritize`, {
        method: "POST",
      });
    } catch (err) {
      console.error("Failed to deprioritize talk:", err);
    }
  }

  function renderEmptyState(queueType, emptyId) {
    if (queueType === "priority") {
      return `
        <div class="queue-empty-state" id="${emptyId}">
          <div class="empty-state-icon-wrap empty-priority">
            <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
            </svg>
          </div>
          <p class="empty-state-title">Priority Queue is Empty</p>
          <p class="empty-state-desc">Drag a talk here to expedite processing ahead of all other tasks.</p>
        </div>
      `;
    }
    if (queueType === "light") {
      return `
        <div class="queue-empty-state" id="${emptyId}">
          <div class="empty-state-icon-wrap empty-light">
            <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
            </svg>
          </div>
          <p class="empty-state-title">No Active Light Jobs</p>
          <p class="empty-state-desc">Trimming, title cards, and previews will appear here when running.</p>
        </div>
      `;
    }
    return `
      <div class="queue-empty-state" id="${emptyId}">
        <div class="empty-state-icon-wrap empty-heavy">
          <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"/>
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"/>
            <line x1="6" y1="6" x2="6.01" y2="6"/>
            <line x1="6" y1="18" x2="6.01" y2="18"/>
          </svg>
        </div>
        <p class="empty-state-title">No Heavy Transcoding Jobs</p>
        <p class="empty-state-desc">Broadcast transcode jobs will appear here when active.</p>
      </div>
    `;
  }

  function renderQueueList(containerId, countId, emptyId, talks, queueType) {
    const container = document.getElementById(containerId);
    const countEl = document.getElementById(countId);
    if (!container) return;

    if (countEl) {
      countEl.textContent = talks ? talks.length : 0;
    }

    if (!talks || talks.length === 0) {
      container.innerHTML = renderEmptyState(queueType, emptyId);
      return;
    }

    container.innerHTML = "";
    talks.forEach((talk) => {
      const card = createCardElement(talk, queueType);
      container.appendChild(card);
    });
  }

  async function fetchQueueData() {
    if (isDragging) return;

    try {
      const res = await fetch("/admin/api/queue");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      renderQueueList("list-light", "count-light", "empty-light", data.light, "light");
      renderQueueList("list-heavy", "count-heavy", "empty-heavy", data.heavy, "heavy");
      renderQueueList("list-priority", "count-priority", "empty-priority", data.priority, "priority");

      const indicator = document.getElementById("queue-live-text");
      if (indicator) indicator.textContent = "Live Syncing";
    } catch (err) {
      console.warn("Queue sync failed:", err);
      const indicator = document.getElementById("queue-live-text");
      if (indicator) indicator.textContent = "Sync Paused";
    }
  }

  function setupGlobalDelegation() {
    document.addEventListener("click", async (e) => {
      const deprioritizeBtn = e.target.closest('[data-action="deprioritize"]');
      if (deprioritizeBtn) {
        const talkId = parseInt(deprioritizeBtn.dataset.talkId, 10);
        if (talkId) {
          await deprioritizeTalk(talkId);
          await fetchQueueData();
        }
        return;
      }

      const prioritizeBtn = e.target.closest('[data-action="prioritize"]');
      if (prioritizeBtn) {
        const talkId = parseInt(prioritizeBtn.dataset.talkId, 10);
        if (talkId) {
          await prioritizeTalk(talkId);
          await fetchQueueData();
        }
        return;
      }
    });

    const refreshBtn = document.getElementById("queue-refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => fetchQueueData());
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupDropZones();
    setupGlobalDelegation();
    fetchQueueData();
    pollInterval = setInterval(fetchQueueData, POLLING_INTERVAL_MS);
  });

  window.addEventListener("beforeunload", () => {
    if (pollInterval) clearInterval(pollInterval);
  });
})();
