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
});
