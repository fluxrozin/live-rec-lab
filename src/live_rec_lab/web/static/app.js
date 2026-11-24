"use strict";

const issuedCsrf = document.querySelector('meta[name="csrf-token"]')?.content;
const flash = document.querySelector("#flash");
const busy = new WeakSet();
let dialogTrigger = null;

function openDialog(dialog, trigger) {
  dialogTrigger = trigger;
  dialog.showModal();
}

function resetMessage(form) {
  const message = form.dataset.message ? document.getElementById(form.dataset.message) : form.querySelector(".form-message");
  message.hidden = true;
  message.classList.remove("error");
}

function showMessage(form, text) {
  const message = form.dataset.message ? document.getElementById(form.dataset.message) : form.querySelector(".form-message");
  message.textContent = text;
  message.classList.add("error");
  message.hidden = false;
}

for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("close", () => {
    dialogTrigger?.focus();
    dialogTrigger = null;
  });
  dialog.addEventListener("cancel", (event) => {
    if (busy.has(dialog.querySelector("form"))) event.preventDefault();
  });
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.open) {
    const dialog = document.getElementById(button.dataset.open);
    const form = dialog.querySelector("form");
    form.reset();
    resetMessage(form);
    openDialog(dialog, button);
  } else if (button.matches("[data-edit], [data-archive]")) {
    const edit = button.hasAttribute("data-edit");
    const id = edit ? button.dataset.edit : button.dataset.archive;
    if (!/^\d+$/.test(id)) return;
    const dialog = document.getElementById(edit ? "edit-streamer" : "archive-streamer");
    const form = dialog.querySelector("form");
    form.reset();
    resetMessage(form);
    form.dataset.api = "/api/v1/streamers/" + id;
    dialog.querySelector("[data-target-label]").textContent = button.dataset.label;
    if (edit) {
      form.elements.display_name.value = button.dataset.display;
      for (const type of ["monitoring", "recording", "comments"]) {
        form.elements[type + "_enabled"].checked = button.dataset[type] === "true";
      }
    }
    openDialog(dialog, button);
  } else if (button.hasAttribute("data-close")) {
    const dialog = button.closest("dialog");
    if (!busy.has(dialog.querySelector("form"))) dialog.close();
  }
});

const numberFields = new Set([
  "refresh_seconds", "revision", "web_port", "tiktok_per_minute", "twitcasting_per_minute",
  "comment_retention_days", "video_retention_days", "minimum_free_gib", "maximum_recording_gib"
]);

function formValues(form) {
  const value = {};
  for (const field of Array.from(form.elements)) {
    if (!field.name) continue;
    value[field.name] = field.type === "checkbox" ? field.checked
      : numberFields.has(field.name) ? Number(field.value)
      : field.name === "recording_root" ? field.value.trim() || null : field.value;
  }
  return value;
}

const settingsBaselines = new Map(Array.from(
  document.querySelectorAll(".preferences-form"),
  (form) => [form, JSON.stringify(formValues(form))]
));
const preferencesForm = document.querySelector("#display-preferences");
if (preferencesForm) {
  const fontPreview = document.querySelector("#font-preview");
  preferencesForm.elements.font.addEventListener("change", (event) => {
    fontPreview.dataset.font = event.target.value;
  });
  preferencesForm.addEventListener("reset", () => {
    queueMicrotask(() => {
      fontPreview.dataset.font = preferencesForm.elements.font.value;
      resetMessage(preferencesForm);
    });
  });
}
if (settingsBaselines.size) {
  window.addEventListener("beforeunload", (event) => {
    if (![...settingsBaselines].some(([form, values]) => JSON.stringify(formValues(form)) !== values)) return;
    event.preventDefault();
    event.returnValue = true;
  });
}

if (flash) {
  try {
    const message = sessionStorage.getItem("live-rec-lab-flash");
    if (message) {
      sessionStorage.removeItem("live-rec-lab-flash");
      flash.textContent = message;
      flash.hidden = false;
    }
  } catch {
    // Settings still work when browser storage is disabled.
  }
}

for (const form of document.querySelectorAll("form[data-method]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.dataset.api || !issuedCsrf || busy.has(form)) return;
    resetMessage(form);
    const submit = event.submitter || Array.from(form.elements).find((field) => field.type === "submit");
    if (!submit) return;
    busy.add(form);
    submit.disabled = true;
    try {
      const response = await fetch(form.dataset.api, {
        method: form.dataset.method,
        headers: {"Content-Type": "application/json", "X-CSRF-Token": issuedCsrf},
        body: JSON.stringify(formValues(form)),
        signal: AbortSignal.timeout(10000)
      });
      if (!response.ok) {
        const result = await response.json();
        showMessage(form, result.error?.message || "操作を受け付けられませんでした。");
        return;
      }
      const result = form.dataset.completedRedirect || form.dataset.reviewMessage ? await response.json() : null;
      if (form.dataset.reviewMessage && result.state === "needs_review") {
        showMessage(form, form.dataset.reviewMessage);
        return;
      }
      if (settingsBaselines.has(form)) settingsBaselines.set(form, JSON.stringify(formValues(form)));
      try {
        sessionStorage.setItem("live-rec-lab-flash", form.dataset.success);
      } catch {
        // The refreshed page still shows the saved values.
      }
      if (form.dataset.completedRedirect && result.state === "completed" && result.mode === "files_and_history") {
        window.location.assign(form.dataset.completedRedirect);
      } else {
        window.location.reload();
      }
    } catch {
      showMessage(form, "結果を確認できませんでした。再送する前に現在の状態を確認してください。");
    } finally {
      submit.disabled = false;
      busy.delete(form);
    }
  });
}

const overview = document.querySelector("#live-overview");
if (overview) {
  const notice = document.querySelector("#refresh-notice");
  const interval = Math.min(Number(overview.dataset.refreshSeconds) * 1000, 2147483647);
  let inProgress = false;
  let nextRefresh;
  async function refreshLive() {
    if (document.hidden || inProgress || overview.contains(document.activeElement)) return;
    inProgress = true;
    try {
      const response = await fetch("/dashboard/live", {signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error("unavailable");
      const result = new DOMParser().parseFromString(await response.text(), "text/html");
      const incoming = result.querySelector("[data-live-fragment]");
      if (!incoming) throw new Error("missing snapshot");
      if (!document.hidden && !overview.contains(document.activeElement)) {
        overview.replaceChildren(incoming);
        const iconPath = incoming.dataset.recording === "true" ? "/static/favicon-recording.svg" : "/static/favicon.svg";
        const icon = document.querySelector('link[rel="icon"]');
        const brandIcon = document.querySelector(".brand img");
        if (icon.getAttribute("href") !== iconPath) {
          icon.setAttribute("href", iconPath);
          brandIcon.setAttribute("src", iconPath);
        }
        notice.hidden = true;
      }
    } catch {
      notice.hidden = false;
    } finally {
      inProgress = false;
    }
  }
  function schedule() {
    window.clearInterval(nextRefresh);
    if (!document.hidden) nextRefresh = window.setInterval(refreshLive, interval);
  }
  document.addEventListener("visibilitychange", () => {
    schedule();
    if (!document.hidden) refreshLive();
  });
  schedule();
}
