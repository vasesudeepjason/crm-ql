(() => {
  const shell = document.querySelector('.crm-shell');
  const sidebar = document.getElementById('sidebar');
  const panel = sidebar.querySelector('.module-panel');
  const currentGroup = () => sidebar.querySelector('button[data-category]');
  const menu = document.getElementById('menuToggle');
  const close = document.getElementById('sidebarClose');
  const backdrop = document.getElementById('sidebarBackdrop');
  const topbar = menu.closest('.mobile-topbar');
  const content = shell.querySelector('main');
  const mobile = window.matchMedia('(max-width: 900px)');

  function renderCategory(expanded) {
    shell.classList.toggle('panel-collapsed', !expanded);
    panel.inert = !expanded;
    currentGroup().setAttribute('aria-expanded', String(expanded));
  }

  function setDrawer(open) {
    sidebar.classList.toggle('open', open);
    backdrop.classList.toggle('open', open);
    document.body.classList.toggle('sidebar-locked', open);
    sidebar.inert = mobile.matches && !open;
    topbar.inert = open;
    content.inert = open;
    menu.setAttribute('aria-expanded', String(open));
    if (open) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
      renderCategory(true);
      currentGroup().focus();
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
    }
  }

  function dismiss() {
    if (mobile.matches) {
      setDrawer(false);
      menu.focus();
    } else {
      renderCategory(false);
      currentGroup().focus();
    }
  }

  sidebar.addEventListener('click', event => {
    const group = event.target.closest('button[data-category]');
    if (!group) return;
    const expanded = mobile.matches || group.getAttribute('aria-expanded') !== 'true';
    renderCategory(expanded);
    if (expanded && event.detail === 0) document.getElementById(group.getAttribute('aria-controls')).querySelector('a').focus();
  });
  menu.addEventListener('click', () => setDrawer(true));
  close.addEventListener('click', dismiss);
  backdrop.addEventListener('click', dismiss);
  sidebar.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      dismiss();
    } else if (event.key === 'Tab' && mobile.matches) {
      const links = [...sidebar.querySelectorAll('a, button')].filter(el => el.getClientRects().length);
      const first = links[0], last = links[links.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  function resetLayout() {
    const hadFocus = sidebar.contains(document.activeElement);
    setDrawer(false);
    renderCategory(true);
    if (mobile.matches && hadFocus) menu.focus();
  }
  mobile.addEventListener('change', resetLayout);
  window.addEventListener('pageshow', event => {
    if (event.persisted) resetLayout();
  });
  resetLayout();

  // Turbo manages requests, history, redirects, and page scripts. Keep the app chrome
  // mounted and take the active navigation state from the server-rendered response.
  document.addEventListener('turbo:before-render', event => {
    const nextBody = event.detail.newBody;
    const nextSidebar = nextBody.querySelector('#sidebar');
    const nextMain = nextBody.querySelector('main.content');
    if (!nextSidebar || !nextMain) return;
    event.detail.render = () => {
      const wasOpen = sidebar.classList.contains('open');
      const focusContent = content.contains(document.activeElement);
      const field = document.activeElement.closest('input[name], select[name], textarea[name]');
      const formAction = field?.form?.action;
      setDrawer(false);
      if (sidebar.dataset.currentCategory !== nextSidebar.dataset.currentCategory) {
        sidebar.dataset.currentCategory = nextSidebar.dataset.currentCategory;
        sidebar.querySelector('.rail-categories').replaceChildren(...nextSidebar.querySelector('.rail-categories').children);
        renderCategory(true);
      }
      for (const group of sidebar.querySelectorAll('.module-group')) {
        const nextGroup = nextSidebar.querySelector('#' + group.id);
        group.hidden = nextGroup.hidden;
        for (const link of group.querySelectorAll('.module-link')) {
          const state = nextGroup.querySelector(`a[href="${CSS.escape(link.getAttribute('href'))}"]`).getAttribute('aria-current');
          if (state) link.setAttribute('aria-current', state);
          else link.removeAttribute('aria-current');
        }
      }
      const identity = sidebar.querySelector('.account-identity');
      const nextIdentity = nextSidebar.querySelector('.account-identity');
      if (!identity.isEqualNode(nextIdentity)) identity.replaceChildren(...nextIdentity.children);
      const notice = content.querySelector('.due-notice');
      nextMain.querySelector('.due-notice').remove();
      content.className = nextMain.className;
      content.replaceChildren(notice, ...nextMain.childNodes);
      if (wasOpen || focusContent) {
        const sameField = formAction && [...content.querySelectorAll('input, select, textarea')].find(
          element => element.name === field.name && element.form?.action === formAction && !element.disabled
        );
        (sameField || content).focus({preventScroll: true});
      }
    };
  });

  document.addEventListener('turbo:before-frame-render', event => {
    const frame = event.target;
    if (frame.id === 'project-views' && frame.contains(document.activeElement) && document.activeElement.matches(':focus-visible')) {
      frame.addEventListener('turbo:frame-render', () => {
        frame.querySelector('.view-switch [aria-current]')?.focus({preventScroll: true});
      }, {once: true});
    }
  });

  document.addEventListener('click', event => {
    const row = event.target.closest('tr.clickable-row');
    if (row && !event.defaultPrevented && !event.target.closest('a, button, input, select, textarea, label') &&
        !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !window.getSelection()?.toString()) {
      row.querySelector('a.row-link')?.click();
    }
  });

  const theme = document.getElementById('themeToggle');
  const root = document.documentElement;
  theme.setAttribute('aria-pressed', String(root.dataset.theme === 'dark'));
  theme.addEventListener('click', () => {
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    theme.setAttribute('aria-pressed', String(next === 'dark'));
    try { localStorage.setItem('leaddesk-theme', next); } catch (e) {}
  });
})();
