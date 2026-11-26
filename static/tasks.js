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
    if (event.target.matches('.tk-attach-input')) {
      uploadFiles(event.target.closest('.tk-panel'), event.target.files);
      event.target.value = '';
      return;
    }
    var form = event.target.closest('form[data-autosave]');
    if (form && form.reportValidity()) form.requestSubmit();
  });

  // ---- attachments: the Attach button, dropping files on the task, pasting into the description ----
  var uploading = false;
  function uploadFiles(panel, fileList) {
    var form = panel && panel.querySelector('[data-task-upload]');
    var files = Array.prototype.slice.call(fileList || []);
    if (!form || !files.length || uploading) return;
    var status = panel.querySelector('[data-upload-status]');
    var data = new FormData();
    data.append('token', form.elements.token.value);
    files.forEach(function (file) { data.append('files', file, file.name || 'pasted-image.png'); });
    uploading = true;
    if (status) { status.classList.remove('is-error'); status.textContent = 'Uploading ' + files.length + (files.length === 1 ? ' file…' : ' files…'); }
    fetch(form.action, { method: 'POST', body: data, credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (response.status === 413) throw new Error('That upload is too large. Add fewer or smaller files.');
        if (!response.ok || response.redirected) throw new Error('The upload could not be saved. Try again.');
        // Saving the task form reloads the panel with the new attachments and keeps any
        // description text that has not been saved yet.
        var main = panel.querySelector('form[data-autosave]');
        if (main && main.reportValidity()) main.requestSubmit();
      })
      .catch(function (error) {
        if (status) { status.classList.add('is-error'); status.textContent = error.message; }
      })
      .then(function () { uploading = false; });
  }

  function hasFiles(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return !!types && Array.prototype.indexOf.call(types, 'Files') !== -1;
  }
  document.addEventListener('dragover', function (event) {
    var main = hasFiles(event) && event.target.closest && event.target.closest('.tk-panel-main');
    if (!main || !main.querySelector('[data-task-upload]')) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    main.classList.add('is-dropping');
  });
  document.addEventListener('dragleave', function (event) {
    var main = event.target.closest && event.target.closest('.tk-panel-main');
    if (main && !main.contains(event.relatedTarget)) main.classList.remove('is-dropping');
  });
  document.addEventListener('drop', function (event) {
    var main = hasFiles(event) && event.target.closest && event.target.closest('.tk-panel-main');
    if (!main || !main.querySelector('[data-task-upload]')) return;
    event.preventDefault();
    main.classList.remove('is-dropping');
    uploadFiles(main.closest('.tk-panel'), event.dataTransfer.files);
  });
  document.addEventListener('paste', function (event) {
    var box = event.target.closest && event.target.closest('.tk-description textarea');
    var files = event.clipboardData && event.clipboardData.files;
    if (!box || !files || !files.length) return;
    event.preventDefault();
    uploadFiles(box.closest('.tk-panel'), files);
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

  // ---- time tracking: keep every running clock on the page ticking ----
  var clockStart = new WeakMap();
  function tickClocks() {
    document.querySelectorAll('[data-timer-elapsed]').forEach(function (el) {
      if (!clockStart.has(el)) clockStart.set(el, Date.now() - Number(el.dataset.timerElapsed) * 1000);
      var total = Math.floor((Date.now() - clockStart.get(el)) / 1000);
      var minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
      el.textContent = Math.floor(total / 3600) + ':' + (minutes < 10 ? '0' : '') + minutes + ':' + (seconds < 10 ? '0' : '') + seconds;
    });
  }
  setInterval(tickClocks, 1000);

  // ---- Gantt: open on today, and come back to where you were after a reload ----
  var ganttLeft = {}, ganttPlaced = new WeakSet();
  function ganttToday(scroller, smooth) {
    // A paged day scale may not include today: open it at its first day instead.
    if (scroller.dataset.today === '') { scroller.scrollLeft = 0; return; }
    var side = scroller.querySelector('.tk-gantt-side');
    var sideWidth = side ? side.offsetWidth : 0;
    var left = Number(scroller.dataset.today) * Number(scroller.dataset.day) - (scroller.clientWidth - sideWidth) / 3;
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    scroller.scrollTo({ left: Math.max(0, left), behavior: smooth && !calm ? 'smooth' : 'auto' });
  }
  function placeGantt() {
    var scroller = document.querySelector('[data-gantt-scroll]');
    if (!scroller || ganttPlaced.has(scroller)) return;
    ganttPlaced.add(scroller);
    var key = location.pathname + location.search;
    if (ganttLeft[key] != null) scroller.scrollLeft = ganttLeft[key];
    else ganttToday(scroller, false);
    scroller.addEventListener('scroll', function () { ganttLeft[key] = scroller.scrollLeft; }, { passive: true });
  }
  document.addEventListener('click', function (event) {
    // Choosing a scale (or Today from another scale) always opens it at today.
    var fresh = event.target.closest('[data-gantt-fresh]');
    if (fresh) {
      var target = new URL(fresh.href, location.href);
      delete ganttLeft[target.pathname + target.search];
      return;
    }
    var jump = event.target.closest('[data-gantt-today]');
    var scroller = jump && document.querySelector('[data-gantt-scroll]');
    if (!scroller) return;
    event.preventDefault();
    ganttToday(scroller, true);
  });
  document.addEventListener('turbo:load', placeGantt);
  placeGantt();

  // ---- Gantt: drag a bar to move it, drag an end to resize it, click or drag an empty row to schedule ----
  // The timeline is counted in steps: one day each on the week, month and quarter scales,
  // 15 minutes each on the day scale (data-unit holds the minutes in one step).
  var drag = null, swallowClick = false, ganttTip = null, ganttGhost = null, lastX = 0, edgeFrame = 0;
  function momentAt(first, steps, minutes) {
    return new Date(Date.parse((first.length === 10 ? first + 'T00:00' : first) + ':00Z') + steps * minutes * 60000);
  }
  function isoAt(first, steps, minutes) { return momentAt(first, steps, minutes).toISOString().slice(0, 16); }
  function dayText(moment) { return moment.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }); }
  function timeText(moment) { return moment.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' }); }
  function pointLabel(first, minutes, step) {
    var moment = momentAt(first, step, minutes);
    return minutes === 1440 ? dayText(moment) : dayText(moment) + ', ' + timeText(moment);
  }
  function rangeLabel(d) {
    var from = momentAt(d.first, d.s, d.minutes);
    if (d.minutes === 1440) {
      return d.n === 1 ? dayText(from) : dayText(from) + ' – ' + dayText(momentAt(d.first, d.s + d.n - 1, 1440)) + ' · ' + d.n + ' days';
    }
    var to = momentAt(d.first, d.s + d.n, d.minutes), total = d.n * d.minutes;
    var length = [total >= 60 ? Math.floor(total / 60) + ' h' : '', total % 60 ? total % 60 + ' min' : ''].join(' ').trim();
    return dayText(from) + ', ' + timeText(from) + ' – ' + (dayText(to) === dayText(from) ? '' : dayText(to) + ', ') + timeText(to) + ' · ' + length;
  }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  // Which step of the timeline is under this x position. Measured from the track as it
  // is right now, so scrolling and page zoom never throw it off.
  function dayAt(track, x) {
    var days = Number(track.closest('[data-gantt-scroll]').dataset.days);
    var box = track.getBoundingClientRect();
    return clamp(Math.floor((x - box.left) / (box.width / days)), 0, days - 1);
  }
  function showTip(track, text, afterDay, above) {
    if (!ganttTip) {
      ganttTip = document.createElement('span');
      ganttTip.className = 'tk-gantt-tip';
      ganttTip.setAttribute('aria-hidden', 'true');
    }
    if (ganttTip.parentNode !== track) track.appendChild(ganttTip);
    ganttTip.textContent = text;
    ganttTip.classList.toggle('is-above', !!above);
    ganttTip.style.setProperty('--s', afterDay);
  }
  function hideHints() {
    if (ganttTip) ganttTip.remove();
    if (ganttGhost) ganttGhost.remove();
  }
  function paintDrag() {
    [drag.bar, drag.label].forEach(function (el) {
      if (!el) return;
      el.style.setProperty('--s', drag.s);
      el.style.setProperty('--n', drag.n);
    });
  }
  function applyDrag(x) {
    var shift = dayAt(drag.track, x) - drag.day0;
    if (drag.mode === 'move') {
      drag.s = clamp(drag.s0 + shift, 0, drag.days - drag.n0);
    } else if (drag.mode === 'start') {
      drag.s = clamp(drag.s0 + shift, 0, drag.s0 + drag.n0 - 1);
      drag.n = drag.n0 + drag.s0 - drag.s;
    } else if (drag.mode === 'end') {
      drag.n = clamp(drag.n0 + shift, 1, drag.days - drag.s0);
    } else {
      var day = clamp(drag.day0 + shift, 0, drag.days - 1);
      drag.s = Math.min(drag.day0, day);
      drag.n = Math.abs(day - drag.day0) + 1;
    }
    paintDrag();
    showTip(drag.track, rangeLabel(drag), dayAt(drag.track, x), true);
  }
  // While dragging near either edge of the timeline, keep it scrolling that way.
  function edgeScroll() {
    edgeFrame = 0;
    if (!drag || !drag.moved) return;
    var scroller = drag.track.closest('[data-gantt-scroll]');
    var box = scroller.getBoundingClientRect();
    var side = scroller.querySelector('.tk-gantt-side').getBoundingClientRect().width;
    var step = lastX > box.right - 36 ? 8 : lastX < box.left + side + 36 ? -8 : 0;
    if (step) { scroller.scrollLeft += step; applyDrag(lastX); }
    edgeFrame = requestAnimationFrame(edgeScroll);
  }
  function endDrag(restore) {
    var d = drag;
    drag = null;
    if (!d) return null;
    if (edgeFrame) { cancelAnimationFrame(edgeFrame); edgeFrame = 0; }
    document.body.classList.remove('tk-gantt-moving', 'tk-gantt-sizing');
    d.track.classList.remove('is-dragging-row');
    if (d.bar) d.bar.classList.remove('is-dragging');
    hideHints();
    if (restore && d.moved) {
      if (d.mode === 'new') d.bar.remove();
      else { d.s = d.s0; d.n = d.n0; drag = d; paintDrag(); drag = null; }
    }
    return d;
  }
  function saveDates(taskId, fields) {
    var tokenHolder = document.querySelector('[data-task-token]');
    var data = new FormData();
    data.append('token', tokenHolder ? tokenHolder.dataset.taskToken : '');
    Object.keys(fields).forEach(function (name) { data.append(name, fields[name]); });
    fetch('/tasks/' + taskId + '/dates', { method: 'POST', body: data, credentials: 'same-origin' })
      .then(function (response) { if (!response.ok) throw new Error('Status ' + response.status); })
      .catch(function () { window.alert('Those dates could not be saved. The timeline will reload.'); })
      .then(refreshList);
  }
  document.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 || !event.target.closest) return;
    var touch = event.pointerType === 'touch';   // fingers can drag bars, but not schedule empty rows
    var track = event.target.closest('.tk-gantt-track');
    var scroller = track && track.closest('[data-gantt-scroll]');
    if (!scroller) return;
    var bar = event.target.closest('.tk-gantt-bar');
    if (bar && !bar.dataset.clipped) {
      var rect = bar.getBoundingClientRect(), edge = Math.min(8, rect.width / 3);
      drag = {
        mode: event.clientX - rect.left < edge ? 'start' : rect.right - event.clientX < edge ? 'end' : 'move',
        bar: bar, label: bar.nextElementSibling, id: bar.dataset.taskId, has: bar.dataset.has,
        s: Number(bar.style.getPropertyValue('--s')), n: Number(bar.style.getPropertyValue('--n'))
      };
    } else if (!touch && !bar && track.dataset.schedule && event.target === track) {
      drag = { mode: 'new', id: track.dataset.schedule, has: 'both', s: dayAt(track, event.clientX), n: 1 };
    } else {
      return;
    }
    drag.track = track; drag.x = lastX = event.clientX; drag.day0 = dayAt(track, event.clientX);
    drag.days = Number(scroller.dataset.days); drag.first = scroller.dataset.first;
    drag.minutes = Number(scroller.dataset.unit) || 1440;
    drag.s0 = drag.s; drag.n0 = drag.n; drag.moved = false;
  });
  document.addEventListener('pointermove', function (event) {
    if (drag) {
      lastX = event.clientX;
      if (!drag.moved) {
        if (Math.abs(event.clientX - drag.x) < 4) return;
        drag.moved = true;
        if (ganttGhost) ganttGhost.remove();
        if (drag.mode === 'new') {
          drag.bar = document.createElement('span');
          drag.bar.className = 'tk-gantt-bar is-new';
          drag.track.appendChild(drag.bar);
        }
        drag.bar.classList.add('is-dragging');
        drag.track.classList.add('is-dragging-row');
        document.body.classList.add(drag.mode === 'move' ? 'tk-gantt-moving' : 'tk-gantt-sizing');
        edgeFrame = requestAnimationFrame(edgeScroll);
      }
      applyDrag(lastX);
      return;
    }
    // Over a task with no dates: a marker follows the pointer and says what a click would set.
    var track = event.pointerType !== 'touch' && event.target.closest && event.target.closest('.tk-gantt-track[data-schedule]');
    if (!track || event.target !== track) { hideHints(); return; }
    var scroller = track.closest('[data-gantt-scroll]');
    var day = dayAt(track, event.clientX);
    if (!ganttGhost) {
      ganttGhost = document.createElement('span');
      ganttGhost.className = 'tk-gantt-ghost';
      ganttGhost.setAttribute('aria-hidden', 'true');
    }
    if (ganttGhost.parentNode !== track) track.appendChild(ganttGhost);
    ganttGhost.style.setProperty('--s', day);
    showTip(track, 'Click or drag to schedule · ' + pointLabel(scroller.dataset.first, Number(scroller.dataset.unit) || 1440, day), day + 1);
  });
  document.documentElement.addEventListener('mouseleave', hideHints);
  document.addEventListener('pointerup', function () {
    var d = endDrag(false);
    if (!d) return;
    if (!d.moved) {
      // A plain click on an empty row schedules the task for that one step: a day, or 15 minutes.
      if (d.mode !== 'new') return;
      d.s = d.day0; d.n = 1;
    } else {
      // The click that follows a drag must not open the task.
      swallowClick = true;
      setTimeout(function () { swallowClick = false; }, 0);
      if (d.mode !== 'new' && d.s === d.s0 && d.n === d.n0) return;
    }
    if (d.minutes !== 1440) {
      saveDates(d.id, { start_at: isoAt(d.first, d.s, d.minutes), end_at: isoAt(d.first, d.s + d.n, d.minutes) });
      return;
    }
    var start = isoAt(d.first, d.s, 1440).slice(0, 10), due = isoAt(d.first, d.s + d.n - 1, 1440).slice(0, 10);
    if (d.mode === 'move' && d.has === 'due') start = '';
    if (d.mode === 'move' && d.has === 'start') due = '';
    saveDates(d.id, { start_date: start, due_date: due });
  });
  document.addEventListener('pointercancel', function () { endDrag(true); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && drag) endDrag(true); });
  document.addEventListener('click', function (event) {
    if (swallowClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    // The due date beside a task name brings its bar into view.
    var jump = event.target.closest && event.target.closest('[data-gantt-jump]');
    var bar = jump && jump.closest('.tk-gantt-row').querySelector('.tk-gantt-bar');
    if (!bar) return;
    var scroller = bar.closest('[data-gantt-scroll]');
    var box = scroller.getBoundingClientRect();
    var side = scroller.querySelector('.tk-gantt-side').getBoundingClientRect().width;
    var scale = box.width / scroller.offsetWidth || 1;
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    scroller.scrollBy({ left: (bar.getBoundingClientRect().left - (box.left + side + 48)) / scale, behavior: calm ? 'auto' : 'smooth' });
  }, true);

  // ---- "Saving…" then "Saved" beside the breadcrumb while the task saves itself ----
  var savedAt = 0;
  var saveTimer = null;
  function saveStatus(text, state) {
    document.querySelectorAll('[data-save-status]').forEach(function (label) {
      label.textContent = text;
      label.dataset.state = state;
    });
  }
  function showSaved() {
    if (Date.now() - savedAt > 4000) return;
    saveStatus('Saved', 'done');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { saveStatus('', ''); }, 2500);
  }
  function isAutosave(event) { return event.target.matches && event.target.matches('form[data-autosave]'); }
  document.addEventListener('turbo:submit-start', function (event) {
    if (isAutosave(event)) saveStatus('Saving…', 'busy');
  });
  document.addEventListener('turbo:submit-end', function (event) {
    if (!isAutosave(event)) return;
    if (event.detail.success) { savedAt = Date.now(); showSaved(); }
    else saveStatus('Not saved. Check your connection and change it again.', 'error');
  });

  document.addEventListener('turbo:frame-load', function (event) {
    // Saving redraws the panel, so the label is put back on the new copy.
    if (event.target.id === FRAME_ID) { scrollFeed(); showSaved(); }
  });
  document.addEventListener('turbo:load', scrollFeed);
  scrollFeed();
})();
