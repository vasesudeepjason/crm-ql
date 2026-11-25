(() => {
  const board = document.querySelector('.planner-board');
  if (!board || board.dataset.boardReady) return;
  board.dataset.boardReady = 'true';
  let card;
  let target;
  function highlight(column) {
    if (target) target.classList.remove('drop-target');
    target = column;
    if (target) target.classList.add('drop-target');
  }
  board.addEventListener('dragstart', event => {
    const ticket = event.target.closest('.planner-ticket[draggable="true"]');
    if (!ticket || event.target !== ticket) return;
    card = ticket;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', ticket.querySelector('.planner-title').textContent);
  });
  board.addEventListener('dragover', event => {
    if (!card) return;
    const column = event.target.closest('.planner-column');
    if (column && !column.contains(card)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      highlight(column);
    } else {
      highlight(null);
    }
  });
  board.addEventListener('drop', event => {
    const column = event.target.closest('.planner-column');
    if (!card || !column || column.contains(card)) return;
    event.preventDefault();
    const form = card.querySelector('.planner-status');
    form.elements.status.value = column.dataset.status;
    form.requestSubmit();
  });
  board.addEventListener('dragend', () => {
    card = null;
    highlight(null);
  });
})();
