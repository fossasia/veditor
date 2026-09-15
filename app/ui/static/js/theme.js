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
    const label = document.getElementById('theme-toggle-label');
    if (label) {
      label.textContent = theme === 'dark' ? 'Light Mode' : 'Dark Mode';
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

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(initial);
    const role = window.getUserRole();
    document.documentElement.setAttribute('data-user-role', role);
    const sel = document.getElementById('user-role-select');
    if (sel) sel.value = role;

    const themeBtn = document.getElementById('theme-toggle-btn');
    if (themeBtn) {
      themeBtn.addEventListener('click', window.toggleTheme);
    }

    // ── User Dropdown Menu (native <details> dismiss & a11y sync) ────
    const userMenu = document.getElementById('user-menu-wrapper');
    const userMenuBtn = document.getElementById('user-menu-btn');
    if (userMenu) {
      const closeUserMenu = () => {
        if (userMenu.open) {
          userMenu.removeAttribute('open');
        }
      };

      if (userMenuBtn) {
        userMenu.addEventListener('toggle', () => {
          userMenuBtn.setAttribute('aria-expanded', String(userMenu.open));
        });
      }

      document.addEventListener('click', (event) => {
        if (userMenu.open && event.target instanceof Node && !userMenu.contains(event.target)) {
          closeUserMenu();
        }
      });

      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && userMenu.open) {
          closeUserMenu();
          event.stopPropagation();
        }
      });
    }
  });
})();

// ── Sidebar Manager ─────────────────────────────────────────────
(function () {
  function isStudioPath(path = window.location.pathname) {
    return path === '/studio' || path.startsWith('/studio/');
  }

  function checkSpeakerStudioMode() {
    if (document.body && document.body.classList.contains('is-speaker')) {
      document.documentElement.setAttribute('data-sidebar', 'hidden');
      return true;
    }
    return false;
  }

  function getInitialSidebarCollapsed() {
    const saved = localStorage.getItem('veditor_sidebar_state');
    if (saved === 'collapsed') return true;
    if (saved === 'expanded') return false;
    // Default to collapsed in Studio mode or on mobile screens
    return isStudioPath() || window.innerWidth < 850;
  }

  function setSidebarCollapsed(collapsed, persist = true) {
    if (document.documentElement.getAttribute('data-sidebar') === 'hidden') {
      return;
    }
    const state = collapsed ? 'collapsed' : 'expanded';
    document.documentElement.setAttribute('data-sidebar', state);
    if (persist) {
      localStorage.setItem('veditor_sidebar_state', state);
    }

    const isExpanded = String(!collapsed);
    const toggleBtn = document.getElementById('sidebar-toggle-btn');
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-expanded', isExpanded);
    }
    const collapseBtn = document.getElementById('sidebar-collapse-btn');
    if (collapseBtn) {
      collapseBtn.setAttribute('aria-expanded', isExpanded);
    }

    window.dispatchEvent(new Event('resize'));
    window.dispatchEvent(new CustomEvent('veditor:sidebar-toggle', { detail: { collapsed } }));
  }

  window.toggleSidebar = function () {
    const isCollapsed = document.documentElement.getAttribute('data-sidebar') === 'collapsed';
    setSidebarCollapsed(!isCollapsed, true);
  };

  window.setSidebarCollapsed = setSidebarCollapsed;

  // Immediate init before DOM paints to prevent flash
  if (!checkSpeakerStudioMode()) {
    const initialCollapse = getInitialSidebarCollapsed();
    document.documentElement.setAttribute('data-sidebar', initialCollapse ? 'collapsed' : 'expanded');
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!checkSpeakerStudioMode()) {
      const shouldCollapse = getInitialSidebarCollapsed();
      setSidebarCollapsed(shouldCollapse, false);

      const toggleBtn = document.getElementById('sidebar-toggle-btn');
      if (toggleBtn) {
        toggleBtn.addEventListener('click', window.toggleSidebar);
      }

      const collapseBtn = document.getElementById('sidebar-collapse-btn');
      if (collapseBtn) {
        collapseBtn.addEventListener('click', () => setSidebarCollapsed(true));
      }
    }
  });
})();

// ── Auth & Role Manager ─────────────────────────────────────────
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
  const isSessionLoggedIn = Boolean(
    document.querySelector('.user-email') ||
    document.getElementById('logout-btn')
  );
  if (!isSessionLoggedIn) {
    const key = window.getApiKey();
    if (key) {
      if (options.headers instanceof Headers) {
        options.headers.set('X-API-Key', key);
      } else {
        options.headers['X-API-Key'] = key;
      }
    }
  }
  options.credentials = options.credentials || 'same-origin';
  return fetch(url, options);
};
