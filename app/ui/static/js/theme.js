/**
 * VEditor Theme & Customization Configuration
 * Supports Light Mode (Default) and Dark Mode with persistence.
 */

window.VEditorConfig = window.VEditorConfig || {
  appName: "VEditor",
  logoText: "VEditor",
  seekSmall: 5,      // 5 seconds
  seekMedium: 30,    // 30 seconds
  seekBig: 60,       // 1 minute (for long talks)
  seekMega: 300,     // 5 minutes (for multi-hour recordings)
  frameRate: 25,
  pollingIntervalMs: 5000,
};

// ── Theme Manager ───────────────────────────────────────────────
(function () {
  function getPreferredTheme() {
    const saved = localStorage.getItem('veditor_theme');
    if (saved === 'dark' || saved === 'light') return saved;
    // Default is light mode
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('veditor_theme', theme);
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) {
      btn.title = theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';
      const sunIcon = document.getElementById('theme-icon-sun');
      const moonIcon = document.getElementById('theme-icon-moon');
      if (sunIcon && moonIcon) {
        sunIcon.style.display = theme === 'dark' ? 'block' : 'none';
        moonIcon.style.display = theme === 'dark' ? 'none' : 'block';
      }
    }
  }

  window.toggleTheme = function () {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
  };

  // Immediate init before DOM paints to prevent flash
  const initial = getPreferredTheme();
  document.documentElement.setAttribute('data-theme', initial);

  function updateKeyIndicatorUI() {
    const hasKey = !!window.getApiKey();
    const btn = document.getElementById('api-key-btn');
    const ind = document.getElementById('api-key-indicator');
    if (btn) {
      if (hasKey) btn.classList.add('key-set');
      else btn.classList.remove('key-set');
    }
    if (ind) {
      ind.textContent = hasKey ? 'API Key Saved' : 'Set API Key';
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(initial);
    const role = window.getUserRole();
    document.documentElement.setAttribute('data-user-role', role);
    const sel = document.getElementById('user-role-select');
    if (sel) sel.value = role;
    updateKeyIndicatorUI();
  });
})();

// ── Auth & Role Manager ─────────────────────────────────────────
window.openApiKeyModal = function () {
  const m = document.getElementById('modal-api-key');
  const input = document.getElementById('api-key-input');
  if (input) input.value = window.getApiKey();
  if (m) m.style.display = 'flex';
};

window.closeApiKeyModal = function () {
  const m = document.getElementById('modal-api-key');
  if (m) m.style.display = 'none';
};

window.saveApiKeyFromModal = function () {
  const input = document.getElementById('api-key-input');
  if (input) {
    const key = input.value.trim();
    window.setApiKey(key);
  }
  window.closeApiKeyModal();
  location.reload();
};

window.promptApiKey = function () {
  window.openApiKeyModal();
};

window.getApiKey = function () {
  return localStorage.getItem('veditor_api_key') || '';
};

window.setApiKey = function (key) {
  localStorage.setItem('veditor_api_key', key);
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = "veditor_api_key=" + encodeURIComponent(key) + "; path=/; max-age=31536000; SameSite=Lax" + secure;
};

window.getUserRole = function () {
  return localStorage.getItem('veditor_role') || 'admin';
};

window.setUserRole = function (role) {
  localStorage.setItem('veditor_role', role);
  document.documentElement.setAttribute('data-user-role', role);
  window.dispatchEvent(new CustomEvent('veditor:role-changed', { detail: { role } }));
};

window.authFetch = function (url, options = {}) {
  options.headers = options.headers || {};
  const key = window.getApiKey();
  if (key) {
    if (options.headers instanceof Headers) {
      options.headers.set('X-API-Key', key);
    } else {
      options.headers['X-API-Key'] = key;
    }
  }
  return fetch(url, options).then(res => {
    if (res.status === 401 && !options._isPolling) {
      window.openApiKeyModal();
    }
    return res;
  });
};
