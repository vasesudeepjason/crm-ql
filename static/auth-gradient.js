// Pointer-following blob and Safari fallback for the sign-in gradient background.
// Ported from the React component's effects: the blob eases one twentieth of the way
// toward the pointer each time the pointer moves, exactly as the original does.
(function () {
  var root = document.querySelector('.ag');
  if (!root) return;

  // Safari renders the SVG "goo" filter poorly, so it gets a plain blur instead.
  if (/^((?!chrome|android).)*safari/i.test(navigator.userAgent)) root.classList.add('is-safari');

  var blob = root.querySelector('.ag-pointer');
  var shell = root.closest('.auth-shell') || root;
  if (!blob) return;

  var curX = 0, curY = 0;
  shell.addEventListener('mousemove', function (event) {
    var rect = blob.getBoundingClientRect();
    var tgX = event.clientX - rect.left;
    var tgY = event.clientY - rect.top;
    curX += (tgX - curX) / 20;
    curY += (tgY - curY) / 20;
    blob.style.transform = 'translate(' + Math.round(curX) + 'px, ' + Math.round(curY) + 'px)';
  });
})();
