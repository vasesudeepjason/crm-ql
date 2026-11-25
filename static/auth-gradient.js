// Sign-in gradient background: random movement.
// Nothing here reacts to the pointer. One blob wanders to a new random spot every few
// seconds, and the five looping blobs start from a random point in their loops so the
// background looks different on every visit.
(function () {
  var root = document.querySelector('.ag');
  if (!root) return;

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  root.querySelectorAll('.ag-blob').forEach(function (blob) {
    var seconds = parseFloat(getComputedStyle(blob).animationDuration) || 30;
    blob.style.animationDelay = '-' + (Math.random() * seconds).toFixed(2) + 's';
  });

  var wanderer = root.querySelector('.ag-wander');
  if (!wanderer) return;
  var STEP_MS = 6000;

  function wander() {
    // The blob is as wide as the longer screen edge and starts centred on the top-left
    // corner, so 0% to 100% of its own size covers the whole background.
    var x = Math.round(Math.random() * 100);
    var y = Math.round(Math.random() * 100);
    wanderer.style.transform = 'translate(' + x + '%, ' + y + '%)';
  }

  wanderer.style.transform = 'translate(' + Math.round(Math.random() * 100) + '%, ' + Math.round(Math.random() * 100) + '%)';
  // Start the first glide a moment later so the starting position is applied without a transition.
  setTimeout(function () {
    wanderer.classList.add('is-moving');
    wander();
  }, 60);
  setInterval(wander, STEP_MS);
})();
