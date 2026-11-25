// Tasks list and task panel. Listeners are delegated from the document so they keep
// working across Turbo page visits; the guard stops them being attached twice.
(function () {
  if (window.__tasksReady) return;
  window.__tasksReady = true;

  var FRAME_ID = 'task-panel';
  var dialog = function () { return document.getElementById('taskDialog'); };
  var frame = function () { return document.getElementById(FRAME_ID); };

  function refreshList() {
    if (window.Turbo) window.Turbo.visit(location.href, { action: 'replace' });
    else location.reload();
  }

  function closeDialog() {
    var d = dialog();
    if (d && d.open) d.close();
  }

  function scrollFeed() {
    document.querySelectorAll('[data-task-feed]').forEach(function (feed) { feed.scrollTop = feed.scrollHeight; });
  }

  document.addEventListener('click', function (event) {
    var target = event.target;

    // Open a task in the dialog. Turbo loads the link into the frame; we show the dialog at once.
    var opener = target.closest('a[data-task-open]');
    if (opener && dialog() && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
      var d = dialog();
      if (!d.open) {
        frame().innerHTML = '<p class="tk-loading">Loading task…</p>';
        d.showModal();
      }
      return;
    }

    if (target.closest('[data-task-close]')) { closeDialog(); return; }

    // A click on the dimmed backdrop lands on the dialog element itself.
    if (target === dialog()) { closeDialog(); return; }

    var twist = target.closest('[data-subs-toggle]');
    if (twist) {
      var subs = document.getElementById(twist.getAttribute('aria-controls'));
      var open = twist.getAttribute('aria-expanded') !== 'true';
      twist.setAttribute('aria-expanded', String(open));
      if (subs) subs.hidden = !open;
      return;
    }

    // New-task dialog, optionally preset with a status (board column) or a due date (calendar day).
    var adder = target.closest('[data-new-task]');
    var maker = document.getElementById('newTaskDialog');
    if (adder && maker) {
      var form = maker.querySelector('form');
      form.reset();
      if (adder.dataset.status) form.elements.status.value = adder.dataset.status;
      if (adder.dataset.due) form.elements.due_date.value = adder.dataset.due;
      maker.showModal();
      form.elements.title.focus();
      return;
    }
    if (target.closest('[data-new-task-cancel]') || target === maker) { if (maker && maker.open) maker.close(); return; }

    // Close an open assignee picker when clicking elsewhere.
    document.querySelectorAll('.tk-picker[open]').forEach(function (picker) {
      if (!picker.contains(target)) picker.open = false;
    });
  });

  // The list behind the dialog is stale after edits, so reload it when the dialog closes.
  document.addEventListener('close', function (event) {
    if (event.target !== dialog()) return;
    var f = frame();
    f.removeAttribute('src');
    f.innerHTML = '';
    refreshList();
  }, true);

  // Save the task form as soon as a field changes.
  document.addEventListener('change', function (event) {
    var form = event.target.closest('form[data-autosave]');
    if (form && form.reportValidity()) form.requestSubmit();
  });

  document.addEventListener('submit', function (event) {
    var message = event.target.getAttribute && event.target.getAttribute('data-confirm');
    if (message && !window.confirm(message)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);

  // Cmd/Ctrl+Enter posts a comment.
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      var form = event.target.closest && event.target.closest('.tk-comment-form');
      if (form) { event.preventDefault(); form.requestSubmit(); }
    }
  });

  // Client-side search across the rows already on the page.
  document.addEventListener('input', function (event) {
    if (!event.target.matches('[data-task-search]')) return;
    var term = event.target.value.trim().toLowerCase();
    var shown = 0;
    document.querySelectorAll('[data-task-group]').forEach(function (group) {
      var count = 0;
      group.querySelectorAll('.tk-rows > [data-task-row]').forEach(function (row) {
        var subs = row.nextElementSibling && row.nextElementSibling.classList.contains('tk-subs') ? row.nextElementSibling : null;
        var match = !term || row.dataset.search.indexOf(term) !== -1;
        var subMatch = false;
        if (subs && term) {
          subs.querySelectorAll('[data-task-row]').forEach(function (sub) {
            var hit = sub.dataset.search.indexOf(term) !== -1;
            sub.hidden = !hit && !match;
            if (hit) subMatch = true;
          });
        } else if (subs) {
          subs.querySelectorAll('[data-task-row]').forEach(function (sub) { sub.hidden = false; });
        }
        row.hidden = !(match || subMatch);
        if (subs && term) {
          subs.hidden = !subMatch;
          var twist = row.querySelector('[data-subs-toggle]');
          if (twist) twist.setAttribute('aria-expanded', String(subMatch));
        }
        if (!row.hidden) count += 1;
      });
      group.hidden = term !== '' && count === 0;
      var badge = group.querySelector('[data-group-count]');
      if (badge) badge.textContent = count;
      shown += count;
    });
    // Board cards and calendar entries.
    document.querySelectorAll('[data-search-item]').forEach(function (item) {
      var hit = !term || item.dataset.search.indexOf(term) !== -1;
      item.hidden = !hit;
      if (hit) shown += 1;
    });
    var empty = document.querySelector('[data-task-nomatch]');
    if (empty) empty.hidden = !(term && shown === 0);
  });

  // Board: drag a card onto a column to change its status.
  var dragged = null;
  document.addEventListener('dragstart', function (event) {
    var card = event.target.closest && event.target.closest('.tk-card');
    if (!card) return;
    dragged = card;
    card.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', card.dataset.taskId);
  });
  document.addEventListener('dragend', function () {
    if (dragged) dragged.classList.remove('is-dragging');
    dragged = null;
    document.querySelectorAll('.tk-col.is-over').forEach(function (col) { col.classList.remove('is-over'); });
  });
  document.addEventListener('dragover', function (event) {
    var col = dragged && event.target.closest && event.target.closest('.tk-col');
    if (!col) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.tk-col.is-over').forEach(function (other) { if (other !== col) other.classList.remove('is-over'); });
    col.classList.add('is-over');
  });
  document.addEventListener('drop', function (event) {
    var col = dragged && event.target.closest && event.target.closest('.tk-col');
    if (!col) return;
    event.preventDefault();
    var card = dragged;
    col.classList.remove('is-over');
    if (card.closest('.tk-col') === col) return;
    moveCard(card, col);
  });

  function moveCard(card, col) {
    var body = col.querySelector('.tk-col-body');
    body.insertBefore(card, body.querySelector('.tk-add'));
    var tokenHolder = document.querySelector('[data-task-token]');
    var data = new FormData();
    data.append('token', tokenHolder ? tokenHolder.dataset.taskToken : '');
    data.append('status', col.dataset.status);
    data.append('next', location.pathname + location.search);
    fetch('/tasks/' + card.dataset.taskId + '/status', { method: 'POST', body: data, credentials: 'same-origin' })
      .then(function (response) { if (!response.ok) throw new Error('Status ' + response.status); })
      .catch(function () { window.alert('That move could not be saved. The board will reload.'); })
      .then(refreshList);
  }

  document.addEventListener('turbo:frame-load', function (event) {
    if (event.target.id === FRAME_ID) scrollFeed();
  });
  document.addEventListener('turbo:load', scrollFeed);
  scrollFeed();
})();
