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
      }
    });
  });

  function closeApiKeysModal() {
    if (apiKeysModal) apiKeysModal.classList.remove('active');
    if (newKeyAlert) newKeyAlert.classList.add('api-key-alert-hidden');
    if (newKeyInput) newKeyInput.value = '';
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

});
