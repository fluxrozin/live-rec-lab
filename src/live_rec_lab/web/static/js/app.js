(function () {
  var root = document.documentElement;
  var preview = document.querySelector("[data-font-preview]");
  if (!document.fonts || (root.dataset.font !== "noto" && !preview)) return;
  document.fonts.load("16px 'Noto Sans JP Probe'", "録画").then(function (faces) {
    root.dataset.fontReady = faces.length ? "yes" : "no";
  }).catch(function () { root.dataset.fontReady = "no"; });
})();

(function () {
  var pointing = false;
  document.addEventListener("pointerdown", function () { pointing = true; });
  ["pointerup", "pointercancel"].forEach(function (name) {
    window.addEventListener(name, function () { pointing = false; });
  });
  window.addEventListener("blur", function () { pointing = false; });

  function pause(target) {
    var active = document.activeElement;
    var selection = document.getSelection();
    return document.hidden || pointing || document.querySelector("dialog[open]") ||
      (active && (active.matches("input, select, textarea, [contenteditable]") || target.contains(active))) ||
      (selection && !selection.isCollapsed);
  }

  function isPoll(target) {
    return target.id === "metrics" || target.hasAttribute("data-recording-comment-count");
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var target = event.detail.elt;
    if (isPoll(target) && pause(target)) event.preventDefault();
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    if (isPoll(target) && pause(target)) event.detail.shouldSwap = false;
  });

  function showCountFailure(event) {
    var target = event.detail.elt;
    if (!target.hasAttribute("data-recording-comment-count")) return;
    var warning = target.querySelector("[data-count-warning]");
    warning.hidden = false;
  }
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.failed) showCountFailure(event);
  });
  ["htmx:sendError", "htmx:timeout", "htmx:sendAbort"].forEach(function (name) {
    document.addEventListener(name, showCountFailure);
  });
})();

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
  var label = logo.querySelector(".brand-status");
  var freshness = document.getElementById("display-freshness");
  var timestamp = document.getElementById("display-asof");
  var icon = document.createElement("link");
  icon.rel = "icon";
  document.head.appendChild(icon);

  function update(state) {
    var recording = state === "yes";
    var unknown = state === "unknown";
    logo.classList.toggle("recording-brand", recording);
    logo.classList.toggle("recording-unknown", unknown);
    label.textContent = recording ? "録画中" : unknown ? "録画状態不明" : "";
    var color = recording ? "#9e263e" : unknown ? "#79500c" : "#204f65";
    var mark = unknown ? '<path fill="white" d="M16 8l8 8-8 8-8-8z"/>' : '<circle cx="16" cy="16" r="5" fill="white"/>';
    icon.href = "data:image/svg+xml," + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="' +
      color + '"/>' + mark + "</svg>"
    );
  }

  update(logo.dataset.recording);
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.target.id !== "metrics") return;
    var metrics = document.getElementById("metrics");
    timestamp.textContent = metrics.dataset.asof;
    freshness.textContent = "最終取得";
    update(metrics.dataset.recording);
  });
  function showDashboardFailure(event) {
    if (event.detail.elt.id !== "metrics") return;
    var warning = document.querySelector("[data-dashboard-warning]");
    warning.textContent = "更新できませんでした。前回取得した稼働状況を表示しています。";
    warning.hidden = false;
    freshness.textContent = "前回取得";
    update("unknown");
  }
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.failed) showDashboardFailure(event);
  });
  ["htmx:sendError", "htmx:timeout", "htmx:sendAbort"].forEach(function (name) {
    document.addEventListener(name, showDashboardFailure);
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
  if (dialog.hasAttribute("data-dialog-reopen")) {
    delete form.dataset.dirty;
    window.location.replace("/streamers" + window.location.search);
    return true;
  }
  form.reset();
  delete form.dataset.dirty;
  form.querySelector("[data-editor-status]").textContent = "";
  return true;
}

document.querySelectorAll("dialog").forEach(function (dialog) {
  if (dialog.hasAttribute("data-dialog-reopen")) dialog.showModal();
  dialog.addEventListener("input", function (event) {
    var form = event.target.closest("[data-dirty-form]");
    if (form) {
      form.dataset.dirty = "true";
      form.querySelector("[data-editor-status]").textContent = "未保存の変更があります";
    }
  });
  dialog.addEventListener("cancel", function (event) {
    if (!canClose(dialog)) event.preventDefault();
  });
});

window.addEventListener("beforeunload", function (event) {
  if (!document.querySelector('dialog[open] [data-dirty-form][data-dirty="true"]:not([aria-busy="true"])')) return;
  event.preventDefault();
  event.returnValue = true;
});

document.querySelectorAll("form[method=post]").forEach(function (form) {
  var submitting = false;
  var submitter, label;
  form.addEventListener("submit", function (event) {
    if (submitting) {
      event.preventDefault();
      return;
    }
    submitting = true;
    form.setAttribute("aria-busy", "true");
    submitter = event.submitter;
    if (submitter) {
      label = submitter.textContent;
      submitter.disabled = true;
      if (!form.hasAttribute("data-settings")) submitter.textContent = "送信中…";
    }
  });
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted || !submitting) return;
    submitting = false;
    form.removeAttribute("aria-busy");
    if (submitter) {
      submitter.disabled = false;
      submitter.textContent = label;
    }
    var status = form.querySelector("[data-settings-status], [data-editor-status]");
    if (status) status.textContent = "送信結果を確認できません。保存済みの内容を確認してください。";
  });
});

(function () {
  function key(url) { return "return-scroll:" + url.pathname + url.search; }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest("a[data-detail-link], a[data-return-scroll]");
    if (!link) return;
    try {
      if (link.hasAttribute("data-detail-link")) {
        sessionStorage.setItem(key(location), JSON.stringify({
          y: window.scrollY,
          detail: link.closest("details")?.dataset.logRow
        }));
      } else {
        sessionStorage.setItem("return-scroll", key(link));
      }
    } catch (_) { /* Browser privacy settings may disable storage. */ }
  });

  try {
    var target = sessionStorage.getItem("return-scroll");
    if (target === key(location)) {
      sessionStorage.removeItem("return-scroll");
      var saved = JSON.parse(sessionStorage.getItem(target));
      sessionStorage.removeItem(target);
      if (saved) {
        var details = document.querySelector("[data-log-row='" + CSS.escape(saved.detail || "") + "']");
        if (details) details.open = true;
        window.scrollTo(0, saved.y);
      }
    }
  } catch (_) { /* Browser privacy settings may disable storage. */ }
})();
