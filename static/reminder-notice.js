(() => {
  const notice = document.querySelector('[data-reminders-url]');
  const link = notice.querySelector('a');
  let loading = false;
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const response = await fetch(notice.dataset.remindersUrl, {cache: 'no-store'});
      if (!response.ok || response.redirected) return;
      const {count, url} = await response.json();
      if (count) {
        const label = count === 1 ? '1 reminder is due' : count + ' reminders are due';
        if (link.textContent !== label) link.textContent = label;
        link.href = url;
      }
      link.hidden = !count;
    } catch {
      // Retry when the connection returns or the tab becomes visible.
    } finally {
      loading = false;
    }
  }
  refresh();
  window.setInterval(refresh, 60000);
  document.addEventListener('turbo:submit-end', event => {
    const form = event.detail.formSubmission.formElement;
    if (event.detail.success && new URL(form.action).pathname.startsWith('/reminders/')) refresh();
  });
  document.addEventListener('visibilitychange', refresh);
  window.addEventListener('online', refresh);
})();
