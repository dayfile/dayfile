// Click (or press Enter on) an event ID badge to copy its short id (the last 6 characters, which `dayfile close`
// accepts); with Shift, the full id. Without scripts the badge's tooltip shows the full id.
(function () {
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {  // plain http away from localhost has no clipboard API
      var box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(box);
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }
  function flash(badge, text) {
    if (badge.dataset.shown === undefined) badge.dataset.shown = badge.textContent;
    badge.textContent = text;
    badge.classList.add("copied");
    clearTimeout(badge.timer);
    badge.timer = setTimeout(function () {
      badge.textContent = badge.dataset.shown;
      badge.classList.remove("copied");
      delete badge.dataset.shown;
    }, 1200);
  }
  function activate(badge, full) {
    var text = full ? badge.dataset.id : badge.dataset.id.slice(-6).toLowerCase();
    copy(text).then(function () { flash(badge, full ? "full id copied" : "copied"); }, function () { flash(badge, "press Ctrl+C"); selectText(badge, text); });
  }
  function selectText(badge, text) {
    badge.textContent = text;
    var range = document.createRange();
    range.selectNodeContents(badge);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  document.addEventListener("click", function (e) {
    var badge = e.target.closest && e.target.closest("[data-id]");
    if (badge) activate(badge, e.shiftKey);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var badge = e.target.closest && e.target.closest("[data-id]");
    if (badge) { e.preventDefault(); activate(badge, e.shiftKey); }
  });
})();
