/* Admin Jobs Monitor: lists jobs from GET /admin/jobs and prioritizes pending ones. */
(function () {
  const tbody = document.getElementById('jobs-tbody');
  const countEl = document.getElementById('jobs-count');
  const emptyEl = document.getElementById('jobs-empty');
  const errorEl = document.getElementById('jobs-error');
  const noticeEl = document.getElementById('jobs-notice');
  const statusSelect = document.getElementById('filter-status');
  const queueSelect = document.getElementById('filter-queue');
  const sortButtons = document.querySelectorAll('.jobs-sort');

  const state = { sort: 'created_at', order: 'desc', requestId: 0 };

  const STATUS_BADGES = {
    queued: 'badge-pending',
    running: 'badge-processing',
    done: 'badge-done',
    failed: 'badge-danger',
    broken: 'badge-danger',
    cancelled: 'badge-waiting',
  };

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function showNotice(message) {
    noticeEl.textContent = message;
    noticeEl.hidden = !message;
  }

  function cell(content, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    if (content instanceof Node) td.appendChild(content);
    else td.textContent = content;
    return td;
  }

  function badge(text, className) {
    const span = document.createElement('span');
    span.className = 'badge ' + className;
    span.textContent = text;
    return span;
  }

  function formatDate(value) {
    return value ? new Date(value).toLocaleString() : '—';
  }

  function renderRow(job) {
    const tr = document.createElement('tr');
    const label = job.job_id !== null ? '#' + job.job_id : (job.rq_job_id || '').slice(0, 8);
    tr.appendChild(cell(label, 'td-mono'));
    tr.appendChild(cell(job.talk_id !== null ? '#' + job.talk_id : '—', 'td-mono'));
    tr.appendChild(cell(job.kind));
    tr.appendChild(cell(badge(job.status, STATUS_BADGES[job.status] || 'badge-waiting')));
    tr.appendChild(cell(job.queue ? badge(job.queue, job.queue.startsWith('priority') ? 'badge-purple' : 'badge-info') : '—'));
    tr.appendChild(cell(job.progress_pct !== null ? Math.round(job.progress_pct) + '%' : '—', 'td-mono'));
    tr.appendChild(cell(formatDate(job.created_at), 'jobs-muted'));

    const actions = cell('', 'col-actions-right');
    if (job.can_prioritize) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-primary btn-sm';
      btn.textContent = 'Prioritize';
      btn.addEventListener('click', () => prioritize(job, btn));
      actions.appendChild(btn);
    }
    tr.appendChild(actions);
    return tr;
  }

  async function loadJobs() {
    // Ignore responses from older requests so they can't overwrite newer filters/sorts.
    const requestId = ++state.requestId;
    const params = new URLSearchParams({ sort: state.sort, order: state.order });
    if (statusSelect.value) params.set('status', statusSelect.value);
    if (queueSelect.value) params.set('queue', queueSelect.value);

    try {
      const res = await window.authFetch('/admin/jobs?' + params.toString());
      if (!res.ok) throw new Error('Failed to load jobs (HTTP ' + res.status + ')');
      const jobs = await res.json();
      if (requestId !== state.requestId) return;
      tbody.replaceChildren(...jobs.map(renderRow));
      countEl.textContent = jobs.length + ' job' + (jobs.length === 1 ? '' : 's');
      emptyEl.hidden = jobs.length > 0;
      showError('');
    } catch (err) {
      if (requestId === state.requestId) showError(err.message);
    }
  }

  async function prioritize(job, btn) {
    btn.disabled = true;
    try {
      const res = await window.authFetch(
        '/admin/jobs/' + encodeURIComponent(job.rq_job_id) + '/prioritize',
        { method: 'POST' }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || 'Failed to prioritize job (HTTP ' + res.status + ')');
      }
      const moved = await res.json();
      showNotice('Moved ' + job.kind + ' job for talk #' + job.talk_id + ' from "' + job.queue + '" to "' + moved.queue + '".');
      showError('');
    } catch (err) {
      showError(err.message);
    }
    await loadJobs();
  }

  function updateSortButtons() {
    sortButtons.forEach((b) => {
      const active = b.dataset.sort === state.sort;
      b.classList.toggle('is-active', active);
      b.classList.toggle('is-asc', active && state.order === 'asc');
    });
  }

  sortButtons.forEach((b) => b.addEventListener('click', () => {
    if (state.sort === b.dataset.sort) {
      state.order = state.order === 'desc' ? 'asc' : 'desc';
    } else {
      state.sort = b.dataset.sort;
      state.order = 'desc';
    }
    updateSortButtons();
    loadJobs();
  }));
  statusSelect.addEventListener('change', loadJobs);
  queueSelect.addEventListener('change', loadJobs);
  document.getElementById('jobs-refresh-btn').addEventListener('click', loadJobs);

  updateSortButtons();
  loadJobs();
  setInterval(loadJobs, 10000);
})();
