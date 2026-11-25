// Leads and Companies lists: open and close their dialogs. Delegated from the document
// so it keeps working across Turbo page visits; the guard stops double-binding.
(function () {
  if (window.__recordsReady) return;
  window.__recordsReady = true;

  document.addEventListener('click', function (event) {
    var opener = event.target.closest('[data-open-dialog]');
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute('data-open-dialog'));
      if (dialog && !dialog.open) {
        dialog.showModal();
        var first = dialog.querySelector('input:not([type=hidden]), select, textarea');
        if (first) first.focus();
      }
      return;
    }
    var closer = event.target.closest('[data-close-dialog]');
    if (closer) {
      var owner = closer.closest('dialog');
      if (owner) owner.close();
      return;
    }
    // A click on the dimmed backdrop lands on the dialog element itself.
    if (event.target.matches && event.target.matches('dialog.rc-dialog')) event.target.close();
  });
})();
