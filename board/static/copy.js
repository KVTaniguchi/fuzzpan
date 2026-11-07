// Copy button for the invite link. Loaded from /static (the CSP blocks inline
// scripts). Without JavaScript the link is still in a readonly field to select.
(function () {
  var button = document.getElementById("copy");
  var field = document.getElementById("link");
  if (!button || !field) return;
  function done() {
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = "Copy link"; }, 3000);
  }
  function fallback() {
    field.focus();
    field.select();
    field.setSelectionRange(0, field.value.length);
    try { if (document.execCommand("copy")) done(); } catch (e) { /* the field stays selected */ }
  }
  button.addEventListener("click", function () {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(done, fallback);
    } else {
      fallback();
    }
  });
})();
