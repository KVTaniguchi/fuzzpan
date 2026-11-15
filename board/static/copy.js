// Copy buttons. Loaded from /static (the CSP blocks inline scripts). A button with
// data-copy="<field id>" copies that field; the invite link page's #copy button
// copies #link. Without JavaScript each value is still in a readonly field to select.
(function () {
  function wire(button, field) {
    if (!button || !field) return;
    var label = button.textContent;
    function done() {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = label; }, 3000);
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
  }
  wire(document.getElementById("copy"), document.getElementById("link"));
  var buttons = document.querySelectorAll("[data-copy]");
  for (var i = 0; i < buttons.length; i++) {
    wire(buttons[i], document.getElementById(buttons[i].getAttribute("data-copy")));
  }
})();
