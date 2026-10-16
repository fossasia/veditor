// ── Unified Modal Controller (Native HTML5 <dialog> with Zero Animation) ──

(function () {
  'use strict';

  const triggerMap = new WeakMap();

  const Modal = {
    open(modalOrId) {
      const dialog = typeof modalOrId === 'string' ? document.getElementById(modalOrId) : modalOrId;
      if (!dialog || !(dialog instanceof HTMLDialogElement)) return;
      if (dialog.open) return;

      if (document.activeElement && document.activeElement !== document.body) {
        triggerMap.set(dialog, document.activeElement);
      }

      dialog.showModal();

      const focusTarget = dialog.querySelector(
        '[autofocus], [data-modal-confirm], input:not([type="hidden"]), select, textarea, button:not([data-modal-close])'
      );
      if (focusTarget && typeof focusTarget.focus === 'function') {
        focusTarget.focus();
      }
    },

    close(modalOrId) {
      const dialog = typeof modalOrId === 'string' ? document.getElementById(modalOrId) : modalOrId;
      if (!dialog || !(dialog instanceof HTMLDialogElement)) return;
      if (!dialog.open) return;

      dialog.close();
      const trigger = triggerMap.get(dialog);
      if (trigger && typeof trigger.focus === 'function') {
        trigger.focus();
        triggerMap.delete(dialog);
      }
    },

    alert(opts) {
      const options = typeof opts === 'string' ? { message: opts } : (opts || {});
      const title = options.title || (options.variant === 'danger' ? 'Error' : 'Notice');
      const message = options.message || '';
      const confirmText = options.confirmText || 'OK';
      const btnVariant = options.variant === 'danger' ? 'btn-danger' : 'btn-primary';

      return new Promise((resolve) => {
        const dialog = document.getElementById('v-dynamic-modal');
        if (!dialog) {
          resolve();
          return;
        }

        dialog.innerHTML = '';
        const card = document.createElement('div');
        card.className = 'v-modal-card v-modal-card-sm';

        const header = document.createElement('div');
        header.className = 'v-modal-header';
        const titleEl = document.createElement('h3');
        titleEl.className = 'v-modal-title';
        titleEl.textContent = title;
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'btn btn-ghost btn-sm';
        closeBtn.setAttribute('data-modal-close', '');
        closeBtn.setAttribute('aria-label', 'Close dialog');
        closeBtn.textContent = '✕';
        header.appendChild(titleEl);
        header.appendChild(closeBtn);

        const body = document.createElement('div');
        body.className = 'v-modal-body';
        const msgEl = document.createElement('p');
        msgEl.className = 'v-modal-message';
        msgEl.textContent = message;
        body.appendChild(msgEl);

        const footer = document.createElement('div');
        footer.className = 'v-modal-footer';
        const okBtn = document.createElement('button');
        okBtn.type = 'button';
        okBtn.className = `btn ${btnVariant} btn-sm`;
        okBtn.setAttribute('data-modal-confirm', '');
        okBtn.textContent = confirmText;
        footer.appendChild(okBtn);

        card.appendChild(header);
        card.appendChild(body);
        card.appendChild(footer);
        dialog.appendChild(card);

        const cleanup = () => {
          dialog.removeEventListener('close', onClose);
          dialog.removeEventListener('click', onClick);
        };

        const onClose = () => {
          cleanup();
          resolve();
        };

        const onClick = (e) => {
          if (e.target.closest('[data-modal-confirm]') || e.target.closest('[data-modal-close]')) {
            Modal.close(dialog);
          }
        };

        dialog.addEventListener('close', onClose);
        dialog.addEventListener('click', onClick);
        Modal.open(dialog);
      });
    },

    confirm(opts) {
      const options = typeof opts === 'string' ? { message: opts } : (opts || {});
      const title = options.title || 'Confirm Action';
      const message = options.message || 'Are you sure you want to proceed?';
      const confirmText = options.confirmText || 'Confirm';
      const cancelText = options.cancelText || 'Cancel';
      const btnVariant = options.variant === 'danger'
        ? 'btn-danger'
        : (options.variant === 'warning' ? 'btn-warning' : 'btn-primary');

      return new Promise((resolve) => {
        const dialog = document.getElementById('v-dynamic-modal');
        if (!dialog) {
          resolve(false);
          return;
        }

        let isConfirmed = false;

        dialog.innerHTML = '';
        const card = document.createElement('div');
        card.className = 'v-modal-card v-modal-card-sm';

        const header = document.createElement('div');
        header.className = 'v-modal-header';
        const titleEl = document.createElement('h3');
        titleEl.className = 'v-modal-title';
        titleEl.textContent = title;
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'btn btn-ghost btn-sm';
        closeBtn.setAttribute('data-modal-close', '');
        closeBtn.setAttribute('aria-label', 'Close dialog');
        closeBtn.textContent = '✕';
        header.appendChild(titleEl);
        header.appendChild(closeBtn);

        const body = document.createElement('div');
        body.className = 'v-modal-body';
        const msgEl = document.createElement('p');
        msgEl.className = 'v-modal-message';
        msgEl.textContent = message;
        body.appendChild(msgEl);

        const footer = document.createElement('div');
        footer.className = 'v-modal-footer';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn btn-ghost btn-sm';
        cancelBtn.setAttribute('data-modal-cancel', '');
        cancelBtn.textContent = cancelText;

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = `btn ${btnVariant} btn-sm`;
        confirmBtn.setAttribute('data-modal-confirm', '');
        confirmBtn.textContent = confirmText;

        footer.appendChild(cancelBtn);
        footer.appendChild(confirmBtn);

        card.appendChild(header);
        card.appendChild(body);
        card.appendChild(footer);
        dialog.appendChild(card);

        const cleanup = () => {
          dialog.removeEventListener('close', onClose);
          dialog.removeEventListener('click', onClick);
        };

        const onClose = () => {
          cleanup();
          resolve(isConfirmed);
        };

        const onClick = (e) => {
          if (e.target.closest('[data-modal-confirm]')) {
            isConfirmed = true;
            Modal.close(dialog);
          } else if (e.target.closest('[data-modal-cancel]') || e.target.closest('[data-modal-close]')) {
            isConfirmed = false;
            Modal.close(dialog);
          }
        };

        dialog.addEventListener('close', onClose);
        dialog.addEventListener('click', onClick);
        Modal.open(dialog);
      });
    }
  };

  // Global backdrop click & close button handler for all .v-modal dialogs
  document.addEventListener('mousedown', (e) => {
    if (e.target instanceof HTMLDialogElement && e.target.classList.contains('v-modal')) {
      e.target._mouseDownOnDialog = true;
    }
  });

  document.addEventListener('click', (e) => {
    // Backdrop click: if clicked directly on the <dialog> element
    if (e.target instanceof HTMLDialogElement && e.target.classList.contains('v-modal')) {
      if (e.target._mouseDownOnDialog && e.target.dataset.dismissible !== 'false') {
        Modal.close(e.target);
      }
      delete e.target._mouseDownOnDialog;
    }

    // Modal close triggers
    const closeBtn = e.target.closest('[data-modal-close], .btn-modal-close');
    if (closeBtn) {
      const dialog = closeBtn.closest('dialog');
      if (dialog) {
        Modal.close(dialog);
      }
    }
  });

  // Restore focus on dialog close (native Escape or dialog.close())
  document.addEventListener(
    'close',
    (e) => {
      if (e.target instanceof HTMLDialogElement) {
        const trigger = triggerMap.get(e.target);
        if (trigger && typeof trigger.focus === 'function') {
          trigger.focus();
          triggerMap.delete(e.target);
        }
      }
    },
    true
  );

  // Expose singleton to window
  window.Modal = Modal;
})();
