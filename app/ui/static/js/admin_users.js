// ── Admin User Management (admin_users.html.jinja) ────────────────

document.addEventListener('DOMContentLoaded', () => {
  const banner = document.getElementById('admin-feedback-banner');
  const bannerText = document.getElementById('admin-feedback-text');
  const bannerClose = document.getElementById('admin-feedback-close');
  let bannerTimer = null;

  function showBanner(message, type) {
    if (!banner || !bannerText) return;
    if (bannerTimer) clearTimeout(bannerTimer);

    bannerText.textContent = message;
    banner.classList.remove('hidden', 'banner-success', 'banner-error');
    banner.classList.add(type === 'error' ? 'banner-error' : 'banner-success');

    bannerTimer = setTimeout(() => {
      banner.classList.add('hidden');
    }, 4500);
  }

  if (bannerClose && banner) {
    bannerClose.addEventListener('click', () => {
      banner.classList.add('hidden');
    });
  }

  // ── Role Promotion / Demotion ──────────────────────────────────
  document.querySelectorAll('.user-role-select').forEach(select => {
    select.addEventListener('change', async () => {
      const userId = select.getAttribute('data-user-id');
      const prevRole = select.getAttribute('data-current-role');
      const newRole = select.value;

      select.disabled = true;

      try {
        const resp = await fetch(`/admin/users/${userId}/promote`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
          },
          body: JSON.stringify({ role: newRole })
        });

        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          const errMsg = errData.detail || `Failed to update role (${resp.status})`;
          showBanner(errMsg, 'error');
          select.value = prevRole;
          return;
        }

        const updated = await resp.json();
        select.setAttribute('data-current-role', updated.role);

        const badge = document.getElementById(`badge-role-${userId}`);
        if (badge) {
          badge.className = `badge badge-role badge-role-${updated.role}`;
          badge.textContent = updated.role.charAt(0).toUpperCase() + updated.role.slice(1);
        }

        showBanner(`User #${userId} role updated to ${updated.role}`, 'success');
      } catch (err) {
        showBanner(`Network error: ${err.message}`, 'error');
        select.value = prevRole;
      } finally {
        select.disabled = false;
      }
    });
  });

  // ── Account Activation / Deactivation ─────────────────────────
  document.querySelectorAll('.btn-toggle-active').forEach(btn => {
    btn.addEventListener('click', async () => {
      const userId = btn.getAttribute('data-user-id');
      const action = btn.getAttribute('data-action');

      if (action === 'deactivate') {
        const confirmed = confirm(
          'Are you sure you want to deactivate this account? ' +
          'The user will be immediately logged out and prevented from signing in.'
        );
        if (!confirmed) return;
      }

      btn.disabled = true;

      try {
        const resp = await fetch(`/admin/users/${userId}/${action}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
          }
        });

        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          const errMsg = errData.detail || `Action failed (${resp.status})`;
          showBanner(errMsg, 'error');
          return;
        }

        const updated = await resp.json();
        const row = document.getElementById(`user-row-${userId}`);
        const statusBadge = document.getElementById(`badge-status-${userId}`);

        if (updated.is_active) {
          btn.textContent = 'Deactivate';
          btn.className = 'btn btn-ghost btn-xs btn-toggle-active btn-deactivate';
          btn.setAttribute('data-action', 'deactivate');

          if (statusBadge) {
            statusBadge.className = 'badge badge-status badge-status-active';
            statusBadge.replaceChildren();
            const dot = document.createElement('span');
            dot.className = 'badge-dot dot-active';
            statusBadge.appendChild(dot);
            statusBadge.appendChild(document.createTextNode('Active'));
          }

          if (row) row.classList.remove('row-inactive');
          showBanner(`User #${userId} account activated`, 'success');
        } else {
          btn.textContent = 'Activate';
          btn.className = 'btn btn-ghost btn-xs btn-toggle-active btn-activate';
          btn.setAttribute('data-action', 'activate');

          if (statusBadge) {
            statusBadge.className = 'badge badge-status badge-status-inactive';
            statusBadge.replaceChildren();
            const dot = document.createElement('span');
            dot.className = 'badge-dot dot-inactive';
            statusBadge.appendChild(dot);
            statusBadge.appendChild(document.createTextNode('Inactive'));
          }

          if (row) row.classList.add('row-inactive');
          showBanner(`User #${userId} account deactivated`, 'success');
        }
      } catch (err) {
        showBanner(`Network error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    });
  });
});
