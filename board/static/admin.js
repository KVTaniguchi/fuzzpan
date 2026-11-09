// Conveniences for the admin page's lane grid. Loaded from /static (the CSP
// blocks inline scripts). Without JavaScript the grid still works: it is one
// form, and the Save button sends every change at once.
(function () {
  var form = document.getElementById("grants");
  if (!form) return;
  var save = document.getElementById("save");
  var status = document.getElementById("dirty");
  var bulk = document.getElementById("bulk");
  var bulkCount = document.getElementById("bulk-count");
  var idle = status.textContent;
  var picks = Array.prototype.slice.call(form.querySelectorAll("input[data-pick]"));
  var pickAll = form.querySelector("input[data-pick-all]");
  var cells = {};  // "read" or "post", then the lane|agent value, to its checkbox
  var boxes = Array.prototype.slice.call(
    form.querySelectorAll('input[name="read"], input[name="post"]'));
  boxes.forEach(function (box) { cells[box.name + "\u0000" + box.value] = box; });
  var saving = false;

  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function refresh() {
    var changed = 0, shared = false;
    boxes.forEach(function (box) {
      var differs = box.checked !== box.defaultChecked;
      box.parentNode.classList.toggle("chg", differs);
      if (differs) {
        changed += 1;
        if (box.checked && box.hasAttribute("data-shared")) shared = true;
      }
    });
    save.disabled = changed === 0;
    status.textContent = changed === 0 ? idle :
      plural(changed, "unsaved change", "unsaved changes") +
      (shared ? ". Adding to a shared lane may need another admin to approve." : ".");
    return changed;
  }

  function selected() { return picks.filter(function (p) { return p.checked; }); }

  function refreshSelection() {
    var chosen = selected();
    picks.forEach(function (p) {
      p.closest("tr").classList.toggle("picked", p.checked);
    });
    pickAll.checked = chosen.length === picks.length;
    bulk.hidden = chosen.length === 0;
    bulkCount.textContent = plural(chosen.length, "agent selected", "agents selected");
  }

  function setRights(agent, lane, read, post) {
    var key = lane + "|" + agent;
    var r = cells["read\u0000" + key], p = cells["post\u0000" + key];
    if (r && !r.disabled) r.checked = read;
    if (p && !p.disabled) p.checked = post;
  }

  picks.forEach(function (p) { p.hidden = false; });
  pickAll.hidden = false;
  refresh();

  form.addEventListener("change", function (event) {
    var t = event.target;
    if (t === pickAll) {
      picks.forEach(function (p) { p.checked = pickAll.checked; });
      refreshSelection();
    } else if (t.hasAttribute("data-pick")) {
      refreshSelection();
    } else if (!t.hasAttribute("data-bulk-lane")) {
      refresh();
    }
  });

  form.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-bulk], button[data-bulk-clear]");
    if (!button) return;
    if (button.hasAttribute("data-bulk-clear")) {
      picks.forEach(function (p) { p.checked = false; });
      refreshSelection();
      return;
    }
    var lanes = Array.prototype.slice.call(form.querySelectorAll("input[data-bulk-lane]"))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return c.getAttribute("data-bulk-lane"); });
    if (lanes.length === 0) {
      status.textContent = "Pick at least one lane to change.";
      return;
    }
    var mode = button.getAttribute("data-bulk");
    selected().forEach(function (p) {
      lanes.forEach(function (lane) {
        setRights(p.getAttribute("data-pick"), lane, mode !== "none", mode === "rp");
      });
    });
    refresh();
  });

  form.addEventListener("submit", function () { saving = true; });
  window.addEventListener("beforeunload", function (event) {
    if (!saving && refresh() > 0) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();
