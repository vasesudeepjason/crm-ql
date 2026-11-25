(() => {
  const workspace = document.querySelector('.chat-workspace');
  if (!workspace || workspace.dataset.chatReady) return;
  workspace.dataset.chatReady = 'true';
  const roster = document.getElementById('chat-roster');
  const search = document.getElementById('chat-search');
  const form = workspace.querySelector('.chat-composer');
  const history = workspace.querySelector('.chat-history');
  const messages = document.getElementById('chat-messages');
  const older = document.getElementById('chat-older');
  const status = document.getElementById('chat-status');
  const peer = workspace.dataset.peer;
  const lifetime = new AbortController();
  const {signal} = lifetime;
  let lastRoster;
  let polling = false;
  let acknowledged = 0;
  let acknowledging = false;
  let sending = false;

  function filterPeople() {
    const query = search.value.trim().toLocaleLowerCase();
    let matches = 0;
    for (const link of roster.querySelectorAll('[data-name]')) {
      link.hidden = !link.dataset.name.toLocaleLowerCase().includes(query);
      if (!link.hidden) matches++;
    }
    document.getElementById('chat-no-match').hidden = !query || matches > 0 || !roster.querySelector('[data-name]');
  }
  search.addEventListener('input', filterPeople);

  const lastId = () => Number(messages?.lastElementChild?.dataset.message || 0);
  const atBottom = () => history && history.scrollHeight - history.scrollTop - history.clientHeight < 48;
  async function markRead() {
    const through = lastId();
    if (signal.aborted || !form || acknowledging || through <= acknowledged || document.hidden || !atBottom()) return;
    acknowledging = true;
    try {
      const response = await fetch(workspace.dataset.read, {method: 'POST',
        body: new URLSearchParams({through, token: form.elements.token.value})});
      if (response.ok && !response.redirected) acknowledged = through;
    } catch {
      // Retry on the next refresh.
    } finally {
      acknowledging = false;
    }
  }

  async function updates(cursor) {
    const url = new URL(workspace.dataset.updates, location.origin);
    url.search = new URLSearchParams({...cursor, peer, view: workspace.dataset.view});
    const response = await fetch(url, {signal, cache: 'no-store', headers: {Accept: 'application/json'}});
    if (!response.ok || response.redirected) throw new Error('Chat refresh failed');
    return response.json();
  }

  function updateRoster(html) {
    if (html === lastRoster) return;
    const focused = roster.contains(document.activeElement) && document.activeElement.dataset.person;
    roster.innerHTML = html;
    lastRoster = html;
    filterPeople();
    if (focused) roster.querySelector('[data-person="' + focused + '"]')?.focus({preventScroll: true});
  }

  async function refresh() {
    if (signal.aborted || polling || document.hidden) return;
    polling = true;
    try {
      let batch;
      do {
        const stick = atBottom();
        batch = await updates({after: lastId()});
        updateRoster(batch.contacts);
        if (peer && batch.last) {
          messages.insertAdjacentHTML('beforeend', batch.messages);
          workspace.querySelector('.chat-empty-thread').hidden = true;
          if (stick) history.scrollTop = history.scrollHeight;
        }
      } while (batch.more && !document.hidden);
      await markRead();
    } catch {
      // Keep the current thread and draft available while disconnected.
    } finally {
      polling = false;
    }
  }

  if (form) {
    history.scrollTop = history.scrollHeight;
    history.addEventListener('scroll', markRead, {passive: true});
    markRead();
    older.addEventListener('click', async () => {
      older.disabled = true;
      try {
        const first = messages.firstElementChild;
        const batch = await updates({before: first.dataset.message});
        messages.insertAdjacentHTML('afterbegin', batch.messages);
        older.hidden = !batch.more;
        first.scrollIntoView({block: 'start'});
        status.textContent = '';
      } catch {
        status.textContent = 'Couldn’t load earlier messages. Try again.';
      } finally {
        older.disabled = false;
      }
    });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (sending) return;
      const input = form.elements.body;
      const draft = input.value;
      if (!draft.trim()) { input.focus(); return; }
      sending = true;
      const send = form.querySelector('[type=submit]');
      send.disabled = true;
      status.textContent = 'Sending…';
      try {
        const response = await fetch(form.action, {method: 'POST', body: new FormData(form),
          headers: {Accept: 'application/json'}});
        if (response.status === 409) {
          const result = await response.json();
          form.elements.client_id.value = result.next_client_id;
          status.textContent = 'The previous message was sent. Choose Send to post your edited text.';
          await refresh();
          return;
        }
        if (!response.ok || response.redirected) throw new Error('Send failed');
        const result = await response.json();
        form.elements.client_id.value = result.next_client_id;
        if (input.value === draft) input.value = '';
        status.textContent = 'Sent';
        history.scrollTop = history.scrollHeight;
        await refresh();
      } catch {
        status.textContent = 'Couldn’t confirm delivery. Retry to check this message.';
      } finally {
        send.disabled = false;
        sending = false;
      }
    });
    form.elements.body.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) form.requestSubmit();
    });

    const toggle = workspace.querySelector('.profile-toggle');
    const dismiss = workspace.querySelector('.profile-dismiss');
    const wide = matchMedia('(min-width: 1181px)');
    function showProfile(open) {
      workspace.dataset.profileOpen = String(open);
      toggle.setAttribute('aria-expanded', String(open));
    }
    function closeProfile() { showProfile(false); toggle.focus(); }
    toggle.addEventListener('click', () => {
      const open = workspace.dataset.profileOpen !== 'true';
      showProfile(open);
      if (open && !wide.matches) dismiss.focus();
    });
    dismiss.addEventListener('click', closeProfile);
    workspace.addEventListener('keydown', event => {
      if (event.key === 'Escape' && workspace.dataset.profileOpen === 'true') closeProfile();
    });
    wide.addEventListener('change', () => showProfile(wide.matches), {signal});
    showProfile(wide.matches);
  }
  const poll = window.setInterval(refresh, 4000);
  document.addEventListener('visibilitychange', refresh, {signal});
  window.addEventListener('online', refresh, {signal});
  document.addEventListener('turbo:before-render', () => {
    clearInterval(poll);
    lifetime.abort();
  }, {once: true});
})();
