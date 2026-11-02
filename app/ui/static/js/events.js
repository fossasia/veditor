// ── Events Management (events.html) ────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const roomsModal = document.getElementById('modal-manage-rooms');
  const roomsList = document.getElementById('rooms-list');
  const roomsEventName = document.getElementById('rooms-event-name-display');
  const roomForm = document.getElementById('create-room-form');
  const roomSubmit = roomForm?.querySelector('button[type="submit"]');
  const roomCancel = document.getElementById('cancel-room-edit');
  let roomsEventId = null;

  function resetRoomForm() {
    roomForm?.reset();
    if (roomForm) delete roomForm.dataset.editingRoomId;
    if (roomSubmit) roomSubmit.textContent = 'Add Room';
    if (roomCancel) roomCancel.hidden = true;
  }

  async function loadRooms() {
    roomsList.textContent = 'Loading rooms...';
    const response = await (window.authFetch || fetch)(`/events/${roomsEventId}/rooms`);
    if (!response.ok) throw new Error(`Failed to load rooms (${response.status})`);
    const rooms = await response.json();
    roomsList.replaceChildren();
    if (!rooms.length) {
      roomsList.textContent = 'No rooms configured.';
      return;
    }
    rooms.forEach(room => {
      const row = document.createElement('div');
      row.className = 'room-list-row';
      const details = document.createElement('div');
      details.innerHTML = `<strong></strong><span></span>`;
      details.querySelector('strong').textContent = room.name;
      const created = room.created_at ? new Date(room.created_at).toLocaleDateString() : 'Unknown date';
      details.querySelector('span').textContent = `${room.description || 'No description'} · ${room.talks_count} talk(s) · Created ${created}`;
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'btn btn-ghost btn-sm';
      edit.textContent = 'Edit';
      edit.addEventListener('click', () => {
        document.getElementById('room-name-input').value = room.name;
        document.getElementById('room-description-input').value = room.description || '';
        roomForm.dataset.editingRoomId = room.id;
        roomSubmit.textContent = 'Save Room';
        roomCancel.hidden = false;
      });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn btn-danger btn-sm';
      remove.textContent = 'Delete';
      remove.addEventListener('click', async () => {
        if (!await Modal.confirm({ title: 'Delete room', message: `Delete “${room.name}”? Talks will keep their records but lose this room assignment.`, confirmText: 'Delete', variant: 'danger' })) return;
        try {
          const result = await (window.authFetch || fetch)(`/events/${roomsEventId}/rooms/${room.id}`, { method: 'DELETE' });
          if (!result.ok) throw new Error('Unable to delete room.');
          await loadRooms();
        } catch (error) {
          Modal.alert({ title: 'Delete room', message: error.message, variant: 'danger' });
        }
      });
      const actions = document.createElement('div');
      actions.className = 'room-list-actions';
      actions.append(edit, remove);
      row.append(details, actions);
      roomsList.append(row);
    });
  }

  document.querySelectorAll('.btn-manage-rooms').forEach(button => {
    button.addEventListener('click', async () => {
      roomsEventId = button.dataset.eventId;
      roomsEventName.textContent = button.dataset.eventName || '';
      resetRoomForm();
      Modal.open(roomsModal);
      try { await loadRooms(); } catch (error) { roomsList.textContent = error.message; }
    });
  });

  roomForm?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const editingRoomId = roomForm.dataset.editingRoomId;
      const response = await (window.authFetch || fetch)(editingRoomId ? `/events/${roomsEventId}/rooms/${editingRoomId}` : `/events/${roomsEventId}/rooms`, {
        method: editingRoomId ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: document.getElementById('room-name-input').value, description: document.getElementById('room-description-input').value || null }),
      });
      if (!response.ok) {
        let body = {};
        try { body = await response.json(); } catch (_) { /* empty response */ }
        const detail = Array.isArray(body.detail) ? body.detail.map(item => item.msg || item).join(', ') : body.detail;
        return Modal.alert({ title: 'Add room', message: detail || 'Unable to add room.', variant: 'danger' });
      }
      roomForm.reset();
      delete roomForm.dataset.editingRoomId;
      roomSubmit.textContent = 'Add Room';
      roomCancel.hidden = true;
      await loadRooms();
    } catch (error) {
      Modal.alert({ title: 'Add room', message: error.message || 'Unable to add room.', variant: 'danger' });
    }
  });

  roomCancel?.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    resetRoomForm();
  });
  roomsModal?.querySelectorAll('[data-modal-close]').forEach(button => {
    button.addEventListener('click', resetRoomForm);
  });
  roomsModal?.addEventListener('close', resetRoomForm);

  const editModal = document.getElementById('modal-edit-event');
  const editForm = document.getElementById('edit-event-form');
  const editInput = document.getElementById('edit-event-name');

  const deleteModal = document.getElementById('modal-delete-event');
  const deleteForm = document.getElementById('delete-event-form');
  const deleteNameDisplay = document.getElementById('delete-event-name-display');

  // Open edit modal
  document.querySelectorAll('.btn-edit-event').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-event-id');
      const name = btn.getAttribute('data-event-name');
      const expireHours = btn.getAttribute('data-event-expire-hours') || '';
      const startDate = btn.getAttribute('data-event-start-date') || '';
      const endDate = btn.getAttribute('data-event-end-date') || '';
      const timezone = btn.getAttribute('data-event-timezone') || '';
      const editExpire = document.getElementById('edit-event-review-window');
      const editTimezone = document.getElementById('edit-event-timezone');
      const editStart = document.getElementById('edit-event-start-date');
      const editEnd = document.getElementById('edit-event-end-date');
      if (editForm && editInput) {
        editForm.action = `/studio/events/${id}/edit`;
        editInput.value = name || '';
      }
      if (editStart) editStart.value = startDate;
      if (editEnd) {
        editEnd.value = endDate;
        if (startDate) {
          editEnd.min = startDate;
        } else {
          editEnd.removeAttribute('min');
        }
      }
      if (editTimezone) {
        if (timezone && !editTimezone.querySelector(`option[value="${timezone}"]`)) {
          const opt = document.createElement('option');
          opt.value = timezone;
          opt.textContent = timezone;
          editTimezone.appendChild(opt);
        }
        editTimezone.value = timezone;
      }
      if (editExpire) {
        if (expireHours && !editExpire.querySelector(`option[value="${expireHours}"]`)) {
          const opt = document.createElement('option');
          opt.value = expireHours;
          opt.textContent = `Custom (${expireHours} hours)`;
          editExpire.appendChild(opt);
        }
        editExpire.value = expireHours;
      }
      if (editModal) {
        Modal.open(editModal);
        if (editInput) editInput.focus();
      }
    });
  });

  // Close edit modal
  document.querySelectorAll('.btn-close-edit-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (editModal) Modal.close(editModal);
    });
  });

  // Open delete modal
  document.querySelectorAll('.btn-delete-event').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-event-id');
      const name = btn.getAttribute('data-event-name') || 'this event';
      if (deleteForm && deleteNameDisplay) {
        deleteForm.action = `/studio/events/${id}/delete`;
        deleteNameDisplay.textContent = `"${name}"`;
      }
      if (deleteModal) {
        Modal.open(deleteModal);
      }
    });
  });

  // Close delete modal
  document.querySelectorAll('.btn-close-delete-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (deleteModal) Modal.close(deleteModal);
    });
  });

  // ── Date Range Sync ─────────────────────────────────────────────────
  function wireDateRangeSync(startId, endId) {
    const startInput = document.getElementById(startId);
    const endInput = document.getElementById(endId);
    if (!startInput || !endInput) return;

    function syncMinMax() {
      if (startInput.value) {
        endInput.min = startInput.value;
        if (endInput.value && endInput.value < startInput.value) {
          endInput.value = startInput.value;
        }
      } else {
        endInput.removeAttribute('min');
      }
    }

    startInput.addEventListener('change', syncMinMax);
    endInput.addEventListener('change', () => {
      if (endInput.value && startInput.value && endInput.value < startInput.value) {
        startInput.value = endInput.value;
        endInput.min = startInput.value;
      }
    });

    syncMinMax();
  }

  wireDateRangeSync('event-start-date', 'event-end-date');
  wireDateRangeSync('edit-event-start-date', 'edit-event-end-date');

  // ── API Keys Modal Handling ─────────────────────────────────────
  const apiKeysModal = document.getElementById('modal-api-keys');
  const apiKeysEventName = document.getElementById('api-keys-event-name-display');
  const apiKeysEventId = document.getElementById('api-keys-event-id-display');
  const apiKeysTbody = document.getElementById('api-keys-tbody');
  const apiKeysEmpty = document.getElementById('api-keys-empty');
  const newKeyAlert = document.getElementById('new-key-alert');
  const newKeyInput = document.getElementById('new-key-input');
  const btnGenerateKey = document.getElementById('btn-generate-api-key');
  const btnCopyNewKey = document.getElementById('btn-copy-new-key');
  const btnCopyEventId = document.querySelector('.btn-copy-event-id');

  // Webhook DOM elements
  const webhookUrlInput = document.getElementById('webhook-url-input');
  const webhookSecretInput = document.getElementById('webhook-secret-input');
  const webhookSecretHint = document.getElementById('webhook-secret-hint');
  const webhookStatusBadge = document.getElementById('webhook-status-badge');
  const webhookTestAlert = document.getElementById('webhook-test-alert');
  const webhookTestMessage = document.getElementById('webhook-test-message');
  const btnToggleSecret = document.getElementById('btn-toggle-secret-visibility');
  const btnGenerateSecret = document.getElementById('btn-generate-webhook-secret');
  const btnCopySecret = document.getElementById('btn-copy-webhook-secret');
  const btnTestWebhook = document.getElementById('btn-test-webhook');
  const btnSaveWebhook = document.getElementById('btn-save-webhook');
  const btnDeleteWebhook = document.getElementById('btn-delete-webhook');

  let currentActiveEventId = null;

  function renderApiKeysStatus(text, isDanger = false) {
    if (!apiKeysTbody) return;
    apiKeysTbody.innerHTML = "";
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = `${isDanger ? "text-danger" : "text-muted"} text-center api-keys-empty-msg`;
    td.textContent = text;
    tr.appendChild(td);
    apiKeysTbody.appendChild(tr);
  }

  async function loadApiKeys(eventId) {
    if (!apiKeysTbody) return;
    renderApiKeysStatus("Loading API keys...");
    if (apiKeysEmpty) apiKeysEmpty.classList.add('api-key-alert-hidden');

    try {
      const resp = await fetch(`/events/${eventId}/api-keys`);
      if (!resp.ok) {
        renderApiKeysStatus(`Failed to load keys (${resp.status})`, true);
        return;
      }
      const keys = await resp.json();
      apiKeysTbody.innerHTML = '';

      if (!keys || keys.length === 0) {
        if (apiKeysEmpty) apiKeysEmpty.classList.remove('api-key-alert-hidden');
        if (btnGenerateKey) {
          btnGenerateKey.textContent = '+ Generate API Key';
          btnGenerateKey.setAttribute('data-has-key', 'false');
        }
        return;
      }

      if (apiKeysEmpty) apiKeysEmpty.classList.add('api-key-alert-hidden');
      if (btnGenerateKey) {
        btnGenerateKey.textContent = '↻ Regenerate Key';
        btnGenerateKey.setAttribute('data-has-key', 'true');
      }

      keys.forEach(k => {
        const createdStr = k.created_at ? new Date(k.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
        const lastUsedStr = k.last_used_at ? new Date(k.last_used_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'Never';
        const tr = document.createElement('tr');

        const tdId = document.createElement('td');
        tdId.className = 'td-mono';
        tdId.textContent = `#${k.id}`;
        tr.appendChild(tdId);

        const tdName = document.createElement('td');
        tdName.textContent = k.name || 'API Key';
        tr.appendChild(tdName);

        const tdKey = document.createElement('td');
        const codeKey = document.createElement('code');
        codeKey.className = 'input-mono';
        codeKey.textContent = k.masked_key;
        tdKey.appendChild(codeKey);
        tr.appendChild(tdKey);

        const tdCreated = document.createElement('td');
        tdCreated.className = 'text-muted';
        tdCreated.style.fontSize = '12px';
        tdCreated.textContent = createdStr;
        tr.appendChild(tdCreated);

        const tdLastUsed = document.createElement('td');
        tdLastUsed.className = 'text-muted';
        tdLastUsed.style.fontSize = '12px';
        tdLastUsed.textContent = lastUsedStr;
        tr.appendChild(tdLastUsed);

        const tdAction = document.createElement('td');
        tdAction.className = 'col-actions-right';
        const btnRevoke = document.createElement('button');
        btnRevoke.type = 'button';
        btnRevoke.className = 'btn btn-ghost btn-xs btn-danger-ghost btn-revoke-key';
        btnRevoke.setAttribute('data-key-id', k.id);
        btnRevoke.textContent = 'Revoke';
        tdAction.appendChild(btnRevoke);
        tr.appendChild(tdAction);

        apiKeysTbody.appendChild(tr);
      });

      // Bind revoke buttons
      document.querySelectorAll('.btn-revoke-key').forEach(b => {
        b.addEventListener('click', async () => {
          const keyId = b.getAttribute('data-key-id');
          const confirmed = await Modal.confirm({
            title: 'Revoke API Key',
            message: 'Are you sure you want to revoke this API key? External integrations using this key will stop working.',
            confirmText: 'Revoke',
            variant: 'danger'
          });
          if (!confirmed) {
            return;
          }
          b.disabled = true;
          b.textContent = 'Revoking...';
          try {
            const delResp = await fetch(`/events/${eventId}/api-keys/${keyId}`, { method: 'DELETE' });
            if (delResp.ok) {
              if (newKeyAlert) newKeyAlert.classList.add('api-key-alert-hidden');
              if (newKeyInput) newKeyInput.value = '';
              await loadApiKeys(eventId);
            } else {
              await Modal.alert({ title: 'Revoke API Key', message: 'Failed to revoke key', variant: 'danger' });
              b.disabled = false;
              b.textContent = 'Revoke';
            }
          } catch (e) {
            await Modal.alert({ title: 'Revoke API Key', message: 'Error revoking key: ' + e, variant: 'danger' });
            b.disabled = false;
            b.textContent = 'Revoke';
          }
        });
      });

    } catch (err) {
      renderApiKeysStatus(`Error loading keys: ${err.message}`, true);
    }
  }



  function showWebhookAlert(message, type = 'info') {
    if (!webhookTestAlert || !webhookTestMessage) return;
    webhookTestAlert.className = `alert alert-${type}`;
    webhookTestMessage.textContent = message;
    webhookTestAlert.classList.remove('webhook-alert-hidden');
  }

  function hideWebhookAlert() {
    if (!webhookTestAlert) return;
    webhookTestAlert.classList.add('webhook-alert-hidden');
    if (webhookTestMessage) webhookTestMessage.textContent = '';
  }

  function setWebhookBadge(status, type = 'neutral') {
    if (!webhookStatusBadge) return;
    webhookStatusBadge.textContent = status;
    webhookStatusBadge.className = `badge badge-${type}`;
  }

  let webhookLoadGeneration = 0;

  async function loadWebhook(eventId) {
    hideWebhookAlert();
    if (!webhookUrlInput || !webhookSecretInput) return;

    const currentGen = ++webhookLoadGeneration;

    try {
      const resp = await fetch(`/events/${eventId}/webhook`);
      if (currentActiveEventId !== eventId || currentGen !== webhookLoadGeneration) return;
      if (!resp.ok) {
        setWebhookBadge('Error', 'neutral');
        return;
      }
      const data = await resp.json();
      if (currentActiveEventId !== eventId || currentGen !== webhookLoadGeneration) return;
      if (data.url) {
        webhookUrlInput.value = data.url;
        if (data.has_secret) {
          webhookSecretInput.value = '';
          webhookSecretInput.placeholder = data.masked_secret ? `Configured (${data.masked_secret})` : '••••••••••••••••';
          if (webhookSecretHint) {
            webhookSecretHint.textContent = `Secret is configured (${data.masked_secret || 'masked'}). Enter a new secret to rotate it.`;
          }
        } else {
          webhookSecretInput.value = '';
          webhookSecretInput.placeholder = 'Enter shared secret or generate one';
          if (webhookSecretHint) {
            webhookSecretHint.textContent = 'No secret configured. Generate or enter one to sign payloads.';
          }
        }
        setWebhookBadge('Active', 'success');
        if (btnDeleteWebhook) btnDeleteWebhook.classList.remove('webhook-alert-hidden');
      } else {
        webhookUrlInput.value = '';
        webhookSecretInput.value = '';
        webhookSecretInput.placeholder = 'Enter shared secret or generate one';
        if (webhookSecretHint) {
          webhookSecretHint.textContent = 'Used by your server to verify payload authenticity. Keep this private.';
        }
        setWebhookBadge('Not Configured', 'neutral');
        if (btnDeleteWebhook) btnDeleteWebhook.classList.add('webhook-alert-hidden');
      }
    } catch (err) {
      setWebhookBadge('Error', 'neutral');
    }
  }

  document.querySelectorAll('.btn-api-keys').forEach(btn => {
    btn.addEventListener('click', () => {
      const eventId = btn.getAttribute('data-event-id');
      const eventName = btn.getAttribute('data-event-name') || `Event #${eventId}`;
      currentActiveEventId = eventId;

      if (apiKeysEventName) apiKeysEventName.textContent = `"${eventName}"`;
      if (apiKeysEventId) apiKeysEventId.textContent = `#${eventId}`;
      if (newKeyAlert) newKeyAlert.classList.add('api-key-alert-hidden');

      if (apiKeysModal) {
        Modal.open(apiKeysModal);
        loadApiKeys(eventId);
        loadWebhook(eventId);
      }
    });
  });

  function cleanupApiKeysModal() {
    if (newKeyAlert) newKeyAlert.classList.add('api-key-alert-hidden');
    if (newKeyInput) newKeyInput.value = '';
    hideWebhookAlert();
    if (webhookUrlInput) webhookUrlInput.value = '';
    if (webhookSecretInput) {
      webhookSecretInput.value = '';
      webhookSecretInput.type = 'password';
    }
    if (btnToggleSecret) btnToggleSecret.textContent = '👁';
    currentActiveEventId = null;
  }

  function closeApiKeysModal() {
    if (apiKeysModal) Modal.close(apiKeysModal);
    cleanupApiKeysModal();
  }

  document.querySelectorAll('.btn-close-api-keys-modal').forEach(btn => {
    btn.addEventListener('click', closeApiKeysModal);
  });

  if (apiKeysModal) {
    apiKeysModal.addEventListener('close', cleanupApiKeysModal);
  }

  if (btnGenerateKey) {
    btnGenerateKey.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      const hasKey = btnGenerateKey.getAttribute('data-has-key') === 'true';
      if (hasKey) {
        const confirmed = await Modal.confirm({
          title: 'Regenerate API Key',
          message: 'An active API key already exists for this event. Regenerating will revoke the current key immediately. Are you sure you want to proceed?',
          confirmText: 'Regenerate',
          variant: 'danger'
        });
        if (!confirmed) {
          return;
        }
      }
      btnGenerateKey.disabled = true;
      btnGenerateKey.textContent = hasKey ? 'Regenerating...' : 'Generating...';

      try {
        const resp = await fetch(`/events/${currentActiveEventId}/api-keys`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'Platform Integration Key' })
        });
        if (resp.ok) {
          const data = await resp.json();
          if (newKeyInput) newKeyInput.value = data.api_key;
          if (newKeyAlert) newKeyAlert.classList.remove('api-key-alert-hidden');
          await loadApiKeys(currentActiveEventId);
        } else {
          await Modal.alert({ title: 'Generate API Key', message: 'Failed to generate key', variant: 'danger' });
        }
      } catch (err) {
        await Modal.alert({ title: 'Generate API Key', message: 'Error generating key: ' + err, variant: 'danger' });
      } finally {
        btnGenerateKey.disabled = false;
        btnGenerateKey.textContent = btnGenerateKey.getAttribute('data-has-key') === 'true' ? '↻ Regenerate Key' : '+ Generate API Key';
      }
    });
  }

  if (btnCopyNewKey && newKeyInput) {
    btnCopyNewKey.addEventListener('click', () => {
      navigator.clipboard.writeText(newKeyInput.value).then(() => {
        const orig = btnCopyNewKey.textContent;
        btnCopyNewKey.textContent = 'Copied!';
        setTimeout(() => { btnCopyNewKey.textContent = orig; }, 2000);
      });
    });
  }

  if (btnCopyEventId) {
    btnCopyEventId.addEventListener('click', () => {
      if (currentActiveEventId) {
        navigator.clipboard.writeText(currentActiveEventId).then(() => {
          const orig = btnCopyEventId.textContent;
          btnCopyEventId.textContent = 'Copied!';
          setTimeout(() => { btnCopyEventId.textContent = orig; }, 2000);
        });
      }
    });
  }

  // ── Webhook Configuration Event Listeners ───────────────────────
  if (btnToggleSecret && webhookSecretInput) {
    btnToggleSecret.addEventListener('click', () => {
      const isPassword = webhookSecretInput.type === 'password';
      webhookSecretInput.type = isPassword ? 'text' : 'password';
      btnToggleSecret.textContent = isPassword ? '🔒' : '👁';
    });
  }

  if (btnGenerateSecret && webhookSecretInput) {
    btnGenerateSecret.addEventListener('click', () => {
      const array = new Uint8Array(24);
      window.crypto.getRandomValues(array);
      const generated = Array.from(array, byte => byte.toString(16).padStart(2, '0')).join('');
      webhookSecretInput.value = generated;
      webhookSecretInput.type = 'text';
      if (btnToggleSecret) btnToggleSecret.textContent = '🔒';
      if (webhookSecretHint) webhookSecretHint.textContent = 'New secret generated. Click "Save Webhook" to apply.';
      showWebhookAlert('Generated new secret. Remember to save your settings.', 'info');
    });
  }

  if (btnCopySecret && webhookSecretInput) {
    btnCopySecret.addEventListener('click', () => {
      const val = webhookSecretInput.value;
      if (!val) {
        showWebhookAlert('No visible secret to copy. If already configured, generate or type a new secret to view/copy.', 'info');
        return;
      }
      navigator.clipboard.writeText(val).then(() => {
        const orig = btnCopySecret.textContent;
        btnCopySecret.textContent = 'Copied!';
        setTimeout(() => { btnCopySecret.textContent = orig; }, 2000);
      }).catch(() => {
        showWebhookAlert('Failed to copy secret to clipboard. Please copy it manually.', 'danger');
      });
    });
  }

  if (btnTestWebhook) {
    btnTestWebhook.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      hideWebhookAlert();

      const url = webhookUrlInput ? webhookUrlInput.value.trim() : '';
      const secret = webhookSecretInput ? webhookSecretInput.value.trim() : '';

      if (!url && btnDeleteWebhook && btnDeleteWebhook.classList.contains('webhook-alert-hidden')) {
        showWebhookAlert('Please enter an endpoint URL before testing.', 'danger');
        return;
      }

      btnTestWebhook.disabled = true;
      btnTestWebhook.textContent = 'Testing...';

      try {
        const body = {};
        if (url) body.url = url;
        if (secret) body.secret = secret;

        const resp = await fetch(`/events/${currentActiveEventId}/webhook/test`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok && data.success) {
          showWebhookAlert(`✓ ${data.message || 'Webhook ping delivered successfully!'}`, 'success');
        } else {
          showWebhookAlert(`✕ ${data.message || data.detail || 'Test ping failed.'}`, 'danger');
        }
      } catch (err) {
        showWebhookAlert(`✕ Error dispatching test webhook: ${err.message}`, 'danger');
      } finally {
        btnTestWebhook.disabled = false;
        btnTestWebhook.textContent = 'Test Webhook';
      }
    });
  }

  if (btnSaveWebhook) {
    btnSaveWebhook.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      hideWebhookAlert();

      const url = webhookUrlInput ? webhookUrlInput.value.trim() : '';
      const secret = webhookSecretInput ? webhookSecretInput.value.trim() : '';

      if (!url) {
        showWebhookAlert('Endpoint URL is required to configure webhooks.', 'danger');
        return;
      }

      btnSaveWebhook.disabled = true;
      btnSaveWebhook.textContent = 'Saving...';

      try {
        const payload = { url };
        if (secret) payload.secret = secret;

        const resp = await fetch(`/events/${currentActiveEventId}/webhook`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (resp.ok) {
          showWebhookAlert('✓ Webhook configuration saved successfully.', 'success');
          await loadWebhook(currentActiveEventId);
          if (secret && webhookSecretInput) {
            webhookSecretInput.value = secret;
          }
        } else {
          const detail = data.detail;
          const msg = Array.isArray(detail) ? detail.map(d => d.msg).join(', ') : (detail || 'Failed to save webhook');
          showWebhookAlert(`✕ ${msg}`, 'danger');
        }
      } catch (err) {
        showWebhookAlert(`✕ Error saving webhook: ${err.message}`, 'danger');
      } finally {
        btnSaveWebhook.disabled = false;
        btnSaveWebhook.textContent = 'Save Webhook';
      }
    });
  }

  if (btnDeleteWebhook) {
    btnDeleteWebhook.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      const confirmed = await Modal.confirm({
        title: 'Remove Webhook',
        message: 'Are you sure you want to remove the outbound webhook configuration for this event?',
        confirmText: 'Remove',
        variant: 'danger'
      });
      if (!confirmed) {
        return;
      }
      hideWebhookAlert();
      btnDeleteWebhook.disabled = true;
      btnDeleteWebhook.textContent = 'Clearing...';

      try {
        const resp = await fetch(`/events/${currentActiveEventId}/webhook`, {
          method: 'DELETE',
        });
        if (resp.ok) {
          showWebhookAlert('Webhook settings cleared.', 'info');
          await loadWebhook(currentActiveEventId);
        } else {
          showWebhookAlert('✕ Failed to clear webhook settings.', 'danger');
        }
      } catch (err) {
        showWebhookAlert(`✕ Error clearing webhook: ${err.message}`, 'danger');
      } finally {
        btnDeleteWebhook.disabled = false;
        btnDeleteWebhook.textContent = 'Clear Webhook';
      }
    });
  }

});
