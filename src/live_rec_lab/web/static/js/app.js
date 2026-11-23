(function () {
  var root = document.documentElement;
  function apply(value) {
    root.dataset.font = value;
    if (value !== "noto" || !document.fonts || !document.fonts.check) {
      delete root.dataset.fontReady;
      return;
    }
    document.fonts.load("16px 'LiveRecorder Noto'", "録画").then(function (faces) {
      root.dataset.fontReady = faces.length ? "yes" : "no";
    }).catch(function () { root.dataset.fontReady = "no"; });
  }
  if (root.dataset.font === "noto") apply("noto");
})();

document.addEventListener("htmx:beforeRequest", function (event) {
  var target = event.detail.elt;
  if (!target.hasAttribute("data-recording-comment-count") && target.id !== "metrics") return;
  var active = document.activeElement;
  var selection = document.getSelection();
  if (document.hidden || document.querySelector("dialog[open]") ||
      (active && (active.matches("input, select, textarea, [contenteditable]") || target.contains(active))) ||
      (selection && !selection.isCollapsed)) {
    event.preventDefault();
  }
});

document.addEventListener("click", async function (event) {
  if (!event.target.matches("[data-log-copy]")) return;
  var panel = event.target.closest(".log-detail");
  var body = panel.querySelector("[data-log-content]");
  var status = panel.querySelector("[role='status']");
  try {
    await navigator.clipboard.writeText(body.textContent);
    status.textContent = "コピーしました";
  } catch (_) {
    var range = document.createRange();
    range.selectNodeContents(body);
    var selection = document.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    status.textContent = "本文を選択しました。ブラウザのコピー操作を使用できます。";
  }
});


(function () {
  var form = document.querySelector("[data-settings]");
  if (!form) return;
  var sections = form.querySelector(".settings-sections");
  var tabs = Array.from(form.querySelectorAll("[data-settings-section]"));
  var panels = form.querySelectorAll("[data-settings-panel]");
  var section = form.elements.section;
  var status = form.querySelector("[data-settings-status]");
  var preview = form.querySelector("[data-font-preview]");
  var dirty = form.dataset.dirty === "true";

  function openSection(value) {
    section.value = value;
    panels.forEach(function (panel) { panel.hidden = panel.dataset.settingsPanel !== value; });
    tabs.forEach(function (tab) {
      if (tab.dataset.settingsSection === value) tab.setAttribute("aria-current", "true");
      else tab.removeAttribute("aria-current");
    });
    form.querySelector("[data-settings-cancel]").href = "/settings?section=" + encodeURIComponent(value);
  }

  sections.hidden = false;
  openSection(section.value);
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { openSection(tab.dataset.settingsSection); });
  });
  form.addEventListener("input", function () {
    dirty = true;
    preview.dataset.fontPreview = form.elements.font.value;
    status.textContent = "未保存の変更があります";
  });
  form.addEventListener("invalid", function (event) {
    var panel = event.target.closest("[data-settings-panel]");
    if (panel) openSection(panel.dataset.settingsPanel);
  }, true);
  window.addEventListener("beforeunload", function (event) {
    if (dirty) {
      event.preventDefault();
      event.returnValue = true;
    }
  });
  form.querySelector("[data-settings-cancel]").addEventListener("click", function (event) {
    if (dirty && !window.confirm("未保存の変更を取り消しますか？")) event.preventDefault();
    else dirty = false;
  });
  form.addEventListener("submit", function () {
    dirty = false;
    form.querySelector("button[type=submit]").disabled = true;
    status.textContent = "保存中";
  });
})();


(function () {
  var logo = document.querySelector(".brand");
  var canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  var context = canvas.getContext("2d");
  var icon = document.createElement("link");
  icon.rel = "icon";
  document.head.appendChild(icon);

  function update() {
    var metrics = document.getElementById("metrics");
    var recording = (metrics ? metrics.dataset.recording : logo.dataset.recording) === "yes";
    logo.classList.toggle("recording-brand", Boolean(recording));
    if (metrics) document.getElementById("display-asof").textContent = metrics.dataset.asof;
    context.clearRect(0, 0, 32, 32);
    context.fillStyle = recording ? "#c93446" : "#4769c8";
    context.beginPath();
    context.arc(16, 16, 12, 0, Math.PI * 2);
    context.fill();
    icon.href = canvas.toDataURL();
  }
  update();
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.target.id === "metrics") update();
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.elt.id !== "metrics" || !event.detail.failed) return;
    var warning = document.querySelector("[data-dashboard-warning]");
    if (warning) {
      warning.textContent = "更新できませんでした。表示中の情報は古い可能性があります。";
      warning.hidden = false;
    }
  });
})();

document.addEventListener("click", function (event) {
  var opener = event.target.closest("[data-dialog-open]");
  if (opener) {
    document.getElementById(opener.dataset.dialogOpen).showModal();
    return;
  }
  var closer = event.target.closest("[data-dialog-close]");
  if (closer) {
    var dialog = closer.closest("dialog");
    if (canClose(dialog)) dialog.close();
  }
});

function canClose(dialog) {
  var form = dialog.querySelector("[data-dirty-form]");
  if (!form || form.dataset.dirty !== "true") return true;
  if (!window.confirm("未保存の変更を取り消しますか？")) return false;
  form.reset();
  delete form.dataset.dirty;
  return true;
}

document.querySelectorAll("dialog").forEach(function (dialog) {
  dialog.addEventListener("input", function (event) {
    var form = event.target.closest("[data-dirty-form]");
    if (form) form.dataset.dirty = "true";
  });
  dialog.addEventListener("cancel", function (event) {
    if (!canClose(dialog)) event.preventDefault();
  });
  dialog.addEventListener("submit", function (event) {
    event.target.querySelectorAll("[type=submit]").forEach(function (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    });
  });
});
