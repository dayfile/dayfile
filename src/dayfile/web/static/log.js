// Opens the Post form in a dialog; without scripts the Post button is a plain link to the form page.
(function () {
  var dialog = document.getElementById("post-dialog");
  var open = document.getElementById("post-open");
  if (!dialog || !open || !dialog.showModal) return;
  open.addEventListener("click", function (e) {
    e.preventDefault();
    dialog.showModal();
    dialog.querySelector("input").focus();
  });
  dialog.querySelectorAll("[data-close]").forEach(function (b) {
    b.addEventListener("click", function () { dialog.close(); });
  });
  dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
})();
