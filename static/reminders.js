// My reminders: the add/edit form opens in a pop-up over the calendar, and the repeat
// options shown follow the chosen "Repeats" value. Listeners are delegated from the document
// so they keep working across Turbo page visits; the guard stops them being attached twice.
(function () {
  if (window.__remindersReady) return;
  window.__remindersReady = true;

  var FRAME_ID = 'reminder-form';
  function dialog() { return document.getElementById('reminderDialog'); }

  function syncForm() {
    document.querySelectorAll('[data-reminder-form]').forEach(function (form) {
      var repeat = form.querySelector('[data-repeat]');
      var unit = form.querySelector('[data-repeat-unit]');
      form.querySelector('[data-repeat-custom]').hidden = repeat.value !== 'custom';
      form.querySelector('[data-repeat-weekdays]').hidden = unit.value !== 'week';
      form.querySelector('[data-repeat-until]').hidden = repeat.value === '';
    });
  }

  function closeDialog() {
    var d = dialog();
    if (d && d.open) d.close();
  }

  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-repeat], [data-repeat-unit]')) syncForm();
  });

  document.addEventListener('click', function (event) {
    var d = dialog();
    if (!d) return;
    var opener = event.target.closest('a[data-reminder-open]');
    if (opener && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
      // Turbo loads the link into the frame; show the pop-up straight away.
      if (!d.open) {
        d.querySelector('turbo-frame').innerHTML = '<p class="reminder-loading">Loading…</p>';
        d.showModal();
      }
      return;
    }
    // Cancel, the close button, or a click on the dimmed backdrop.
    if (d.open && (event.target.closest('[data-reminder-close]') || event.target === d)) {
      event.preventDefault();
      closeDialog();
    }
  });

  document.addEventListener('close', function (event) {
    if (event.target !== dialog()) return;
    var frame = event.target.querySelector('turbo-frame');
    frame.removeAttribute('src');
    frame.innerHTML = '';
  }, true);

  // A saved reminder redirects to the calendar: close the pop-up and show that page.
  document.addEventListener('turbo:submit-end', function (event) {
    var form = event.target;
    var d = dialog();
    if (!d || !form.matches || !form.matches('[data-reminder-form]') || !d.contains(form) || !event.detail.success) return;
    var response = event.detail.fetchResponse && event.detail.fetchResponse.response;
    closeDialog();
    if (window.Turbo) window.Turbo.visit(response && response.url ? response.url : location.href, { action: 'replace' });
    else location.reload();
  });

  document.addEventListener('turbo:frame-load', function (event) {
    if (event.target.id !== FRAME_ID) return;
    syncForm();
    var first = event.target.querySelector('input[name="title"]');
    if (first && dialog() && dialog().open) first.focus();
  });
  document.addEventListener('turbo:load', syncForm);
  syncForm();
})();
