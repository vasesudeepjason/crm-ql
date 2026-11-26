// Live notifications: sidebar badges, tab title, toasts, and instant chat updates.
// Each tab keeps one WebSocket open across Turbo page visits and the server pushes changes
// over it. If a socket cannot be opened (library missing, proxy not passing upgrades, or a
// server that cannot hold connections), the page asks the server every few seconds instead
// and tries the socket again later.
(function () {
  if (window.__liveReady) return;
  window.__liveReady = true;

  var POLL_MS = 5000;
  var RETRY_SOCKET_MS = 120000;   // while polling, how often to try the socket again
  var TOAST_MS = 7000;
  var MAX_TOASTS = 4;

  var counts = null;        // latest {chat, tasks, all}
  var cursors = null;       // {chat, note}: the newest items already seen
  var socket = null;
  var everOpened = false;
  var failures = 0;
  var reconnectTimer = null;
  var pollTimer = null;
  var polling = false;
  var baseTitle = document.title;

  function host() { return document.getElementById('live-toasts'); }
  function storageKey() { return 'live-cursors-' + (host() ? host().dataset.user : ''); }

  function loadCursors() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(storageKey()) || 'null');
      if (saved && Number.isFinite(saved.chat) && Number.isFinite(saved.note)) return saved;
    } catch (error) { /* private mode or blocked storage: start from now */ }
    return null;
  }
  function saveCursors() {
    try { sessionStorage.setItem(storageKey(), JSON.stringify(cursors)); } catch (error) { /* not essential */ }
  }

  // ---- badges and tab title ----
  function render() {
    if (!counts) return;
    document.querySelectorAll('[data-live-badge]').forEach(function (badge) {
      var value = counts[badge.dataset.liveBadge] || 0;
      badge.textContent = value > 99 ? '99+' : String(value);
      badge.setAttribute('aria-label', value + ' unread');
      badge.hidden = value === 0;
    });
    var prefix = '';
    if (counts.chat > 0) prefix = '(' + counts.chat + ') New unread message' + (counts.chat === 1 ? '' : 's') + ' · ';
    else if (counts.tasks > 0) prefix = '(' + counts.tasks + ') New task' + (counts.tasks === 1 ? '' : 's') + ' assigned · ';
    var wanted = prefix + baseTitle;
    if (document.title !== wanted) document.title = wanted;
  }

  function rememberTitle() {
    // Each page arrives with its own plain title; keep it so the unread prefix can be re-applied.
    baseTitle = document.title.replace(/^\(\d+\) New (unread messages?|tasks? assigned) · /, '');
    render();
  }

  // ---- toasts ----
  function toast(kind, title, body, href) {
    var area = host();
    if (!area) return;
    while (area.children.length >= MAX_TOASTS) area.firstElementChild.remove();

    var item = document.createElement('div');
    item.className = 'live-toast is-' + kind;
    var link = document.createElement('a');
    link.href = href;
    link.className = 'live-toast-body';
    var heading = document.createElement('strong');
    heading.textContent = title;
    var text = document.createElement('span');
    text.textContent = body || '';
    link.append(heading, text);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'live-toast-close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '×';
    item.append(link, close);
    area.append(item);

    var timer = window.setTimeout(function () { item.remove(); }, TOAST_MS);
    close.addEventListener('click', function () { window.clearTimeout(timer); item.remove(); });
    link.addEventListener('click', function () { window.clearTimeout(timer); item.remove(); });
    item.addEventListener('mouseenter', function () { window.clearTimeout(timer); });
    item.addEventListener('mouseleave', function () { timer = window.setTimeout(function () { item.remove(); }, 3000); });
  }

  function viewingChatWith(senderId) {
    var workspace = document.querySelector('.chat-workspace');
    return !!workspace && !document.hidden && String(workspace.dataset.peer) === String(senderId);
  }

  // ---- incoming state ----
  function handle(state) {
    var area = host();
    var first = cursors === null;
    var before = counts;
    counts = { chat: state.chat, tasks: state.tasks, all: state.all };
    cursors = { chat: state.chat_cursor, note: state.note_cursor };
    saveCursors();

    if (!first && area) {
      (state.chats || []).forEach(function (message) {
        if (!viewingChatWith(message.from)) {
          toast('chat', message.name, message.body, area.dataset.chatUrl + '/' + message.from);
        }
      });
      (state.notes || []).forEach(function (note) {
        toast('task', note.title, note.body, note.task ? area.dataset.taskUrl + '/' + note.task : area.dataset.taskUrl);
      });
    }
    if ((state.chats && state.chats.length) || !before || before.chat !== counts.chat) {
      document.dispatchEvent(new CustomEvent('crm:chat-activity'));
    }
    render();
  }

  function query() {
    var have = cursors || loadCursors();
    return '?chat_after=' + (have ? have.chat : -1) + '&note_after=' + (have ? have.note : -1);
  }

  // ---- fallback: ask every few seconds ----
  function poll() {
    var area = host();
    if (!area || document.hidden) return;
    fetch(area.dataset.stateUrl + query(), { cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (response) { return response.ok && !response.redirected ? response.json() : null; })
      .then(function (state) { if (state) handle(state); })
      .catch(function () { /* offline: try again on the next tick */ });
  }
  function startPolling() {
    if (polling) return;
    polling = true;
    poll();
    pollTimer = window.setInterval(poll, POLL_MS);
  }
  function stopPolling() {
    polling = false;
    window.clearInterval(pollTimer);
    pollTimer = null;
  }

  // ---- preferred: a WebSocket the server pushes over ----
  function socketUrl() {
    var area = host();
    if (!area || !area.dataset.socketUrl || !window.WebSocket) return null;
    var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    return scheme + location.host + area.dataset.socketUrl + query();
  }

  function schedule(delay) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = window.setTimeout(connect, delay);
  }

  function connect() {
    var area = host();
    if (!area || socket) return;
    if (cursors === null) cursors = loadCursors();
    var url = socketUrl();
    if (!url) { startPolling(); return; }

    var opened = false;
    var mine = socket = new WebSocket(url);
    mine.onopen = function () {
      opened = everOpened = true;
      failures = 0;
      stopPolling();
    };
    mine.onmessage = function (event) {
      try { handle(JSON.parse(event.data)); } catch (error) { /* ignore a malformed message */ }
    };
    mine.onclose = function (event) {
      if (socket !== mine) return;   // we closed it ourselves
      socket = null;
      // 1013: the server cannot hold sockets open. 1008: not signed in. Either way, ask instead.
      var refused = event.code === 1013 || event.code === 1008;
      if (opened && !refused) {
        schedule(Math.min(10000, 1000 * Math.pow(2, failures++)));   // dropped connection: reconnect soon
        return;
      }
      failures += 1;
      if (refused || failures >= 2) {
        startPolling();
        schedule(RETRY_SOCKET_MS);
      } else {
        schedule(1500);
      }
    };
  }

  function disconnect() {
    window.clearTimeout(reconnectTimer);
    if (socket) {
      var closing = socket;
      socket = null;
      closing.close();
    }
  }

  document.addEventListener('turbo:load', function () { rememberTitle(); connect(); });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (polling) poll();
    if (!socket) connect();
  });
  window.addEventListener('online', function () { if (polling) poll(); if (!socket) connect(); });
  window.addEventListener('pagehide', disconnect);
  window.addEventListener('pageshow', connect);
  rememberTitle();
  connect();
})();
