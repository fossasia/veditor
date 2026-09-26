// ── Events Management (events.html) ────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
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
      if (editForm && editInput) {
        editForm.action = `/studio/events/${id}/edit`;
        editInput.value = name || '';
      }
      if (editModal) {
        editModal.classList.add('active');
        if (editInput) editInput.focus();
      }
    });
  });

  // Close edit modal
  document.querySelectorAll('.btn-close-edit-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (editModal) editModal.classList.remove('active');
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
        deleteModal.classList.add('active');
      }
    });
  });

  // Close delete modal
  document.querySelectorAll('.btn-close-delete-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (deleteModal) deleteModal.classList.remove('active');
    });
  });

  // Close modals when clicking backdrop
  [editModal, deleteModal].forEach(modal => {
    if (modal) {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) {
          modal.classList.remove('active');
        }
      });
    }
  });

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
          if (!confirm('Are you sure you want to revoke this API key? External integrations using this key will stop working.')) {
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
              alert('Failed to revoke key');
              b.disabled = false;
              b.textContent = 'Revoke';
            }
          } catch (e) {
            alert('Error revoking key: ' + e);
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

  async function loadWebhook(eventId) {
    hideWebhookAlert();
    if (!webhookUrlInput || !webhookSecretInput) return;

    try {
      const resp = await fetch(`/events/${eventId}/webhook`);
      if (currentActiveEventId !== eventId) return;
      if (!resp.ok) {
        setWebhookBadge('Error', 'neutral');
        return;
      }
      const data = await resp.json();
      if (currentActiveEventId !== eventId) return;
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
        apiKeysModal.classList.add('active');
        loadApiKeys(eventId);
        loadWebhook(eventId);
      }
    });
  });

  function closeApiKeysModal() {
    if (apiKeysModal) apiKeysModal.classList.remove('active');
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

  document.querySelectorAll('.btn-close-api-keys-modal').forEach(btn => {
    btn.addEventListener('click', closeApiKeysModal);
  });

  if (apiKeysModal) {
    apiKeysModal.addEventListener('click', (e) => {
      if (e.target === apiKeysModal) {
        closeApiKeysModal();
      }
    });
  }

  if (btnGenerateKey) {
    btnGenerateKey.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      const hasKey = btnGenerateKey.getAttribute('data-has-key') === 'true';
      if (hasKey) {
        if (!confirm('An active API key already exists for this event. Regenerating will revoke the current key immediately. Are you sure you want to proceed?')) {
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
          alert('Failed to generate key');
        }
      } catch (err) {
        alert('Error generating key: ' + err);
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
      if (!confirm('Are you sure you want to remove the outbound webhook configuration for this event?')) {
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
