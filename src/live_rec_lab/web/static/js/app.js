/* LiveRecorder 画面スクリプト（標準HTML + 最小JS）。変更操作は /api/v1 を CSRFヘッダ付きで呼ぶ。 */
(function () {
  "use strict";
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  function toast(msg, isErr) {
    var root = document.getElementById("toast-root");
    if (!root) return;
    var el = document.createElement("div");
    el.className = "c-toast" + (isErr ? " c-toast--err" : "");
    el.textContent = msg;
    if (isErr) {  // 重要エラーは手動で閉じる
      var b = document.createElement("button");
      b.className = "c-btn c-btn--sm c-btn--ghost"; b.textContent = "閉じる"; b.style.color = "#fff";
      b.onclick = function () { el.remove(); };
      el.appendChild(document.createTextNode(" ")); el.appendChild(b);
    } else { setTimeout(function () { el.remove(); }, 3500); }
    root.appendChild(el);
  }

  function api(method, url, body) {
    var opt = { method: method, headers: { "X-CSRF-Token": CSRF } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
    });
  }

  var streamerWrites = 0;
  async function streamerWrite(save) {
    streamerWrites++;
    document.dispatchEvent(new Event("streamer-write"));
    try {
      return await save();
    } finally {
      streamerWrites--;
      document.dispatchEvent(new Event("streamer-write"));
    }
  }

  function errText(res) {
    var e = (res.body && res.body.error) || {};
    var f = e.fields ? " (" + Object.keys(e.fields).map(function (k) { return k + ": " + e.fields[k]; }).join(" / ") + ")" : "";
    return (e.message || "失敗しました") + f;
  }

  /* ---- 確認ダイアログ ---- */
  var pending = null;
  function confirmDialog(title, body, okLabel, withInput, initial) {
    return new Promise(function (resolve) {
      var d = document.getElementById("confirm-dialog");
      if (!d) { resolve(null); return; }
      d.querySelector("#confirm-title").textContent = title;
      var bodyEl = d.querySelector("#confirm-body");
      bodyEl.textContent = body || "";
      var input = d.querySelector("#confirm-input");
      if (input) input.remove();
      if (withInput) {
        input = document.createElement("input"); input.id = "confirm-input"; input.className = "c-input"; input.style.width = "100%";
        input.value = initial || ""; input.maxLength = 100; bodyEl.after(input);
      }
      d.querySelector('[data-action="confirm-ok"]').textContent = okLabel || "実行";
      pending = function (ok) { d.close(); pending = null; resolve(ok ? (withInput ? input.value : true) : null); };
      d.showModal();
    });
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action]");
    if (!t) return;
    var a = t.dataset.action;
    if (a === "confirm-ok" && pending) return pending(true);
    if (a === "confirm-cancel" && pending) return pending(false);
    if (a === "open-add") { var d = document.getElementById("add-dialog"); d.querySelector("form").reset(); document.getElementById("add-error").textContent = ""; d.showModal(); return; }
    if (a === "close-add") { document.getElementById("add-dialog").close(); return; }
    var row = t.closest("tr[data-id]");
    if (a === "delete" && row) {
      confirmDialog("配信者を削除", "「" + t.dataset.name + "」を一覧から削除します。過去の録画・コメント履歴は残ります。", "削除").then(function (ok) {
        if (!ok) return;
        streamerWrite(() => api("DELETE", "/api/v1/streamers/" + row.dataset.id + "?confirm=true")).then(function (r) {
          if (r.ok) { toast("削除しました"); location.reload(); } else toast(errText(r), true);
        });
      });
    }
    if (a === "edit" && row) {
      confirmDialog("表示名を編集", "", "保存", true, t.dataset.name).then(function (v) {
        if (v === null) return;
        streamerWrite(() => api("PATCH", "/api/v1/streamers/" + row.dataset.id, { display_name: v })).then(function (r) {
          if (r.ok) { toast("保存しました"); location.reload(); } else toast(errText(r), true);
        });
      });
    }
    if (a === "move" && row) {
      var rows = Array.prototype.slice.call(document.querySelectorAll("#streamer-rows tr"));
      var i = rows.indexOf(row), j = i + parseInt(t.dataset.dir, 10);
      if (j < 0 || j >= rows.length) return;
      var ids = rows.map(function (r) { return parseInt(r.dataset.id, 10); });
      var tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp;
      streamerWrite(async function () {
        var cur = await api("GET", "/api/v1/streamers/order");
        return api("PUT", "/api/v1/streamers/order", { ordered_ids: ids, order_revision: cur.body.order_revision });
      }).then(function (r) { if (r.ok) location.reload(); else toast(errText(r), true); });
    }
    if (a === "stop-recording") {
      confirmDialog("録画を停止", "この配信の録画を停止します。同じ配信は自動では再開されません。", "停止").then(function (ok) {
        if (!ok) return;
        api("POST", "/api/v1/sessions/" + t.dataset.id + "/stop").then(function (r) {
          if (r.ok) toast("停止を受け付けました"); else toast(errText(r), true);
        });
      });
    }
    if (a === "resume") {
      var label = t.dataset.scope === "recording" ? "録画" : "コメント収集";
      confirmDialog(label + "を再開", "同じ配信で" + label + "を再開します。新しい試行予算で開始します。", "再開").then(function (ok) {
        if (!ok) return;
        api("POST", "/api/v1/sessions/" + t.dataset.id + "/resume", { confirm: true, scope: t.dataset.scope }).then(function (r) {
          if (r.ok) toast(r.body.result === "noop" ? "既に稼働中です" : "再開を受け付けました"); else toast(errText(r), true);
        });
      });
    }
    if (a === "delete-archive") {
      confirmDialog("アーカイブを削除", "「" + t.dataset.name + "」の動画・全字幕・コメント・履歴を一括で削除します。取り消せません。", "削除する").then(function (ok) {
        if (!ok) return;
        api("DELETE", "/api/v1/sessions/" + t.dataset.id + "?confirm=true").then(function (r) {
          if (r.ok) { toast("削除しました"); location.href = "/recordings"; } else toast(errText(r), true);
        });
      });
    }
    if (a === "manual-converge") {
      confirmDialog("手動収束を確定", "終了の根拠は未確認です。録画・コメントが再開しないことを確認した場合のみ確定してください。理由を入力:", "確定", true, "").then(function (v) {
        if (!v) return;
        api("POST", "/api/v1/sessions/" + t.dataset.id + "/manual-convergence", { confirm: true, reason: v }).then(function (r) {
          if (r.ok) { toast("手動収束を記録しました"); location.reload(); } else toast(errText(r), true);
        });
      });
    }
    if (a === "start-scan") {
      api("POST", "/api/v1/system/scans").then(function (r) { if (r.ok) { toast("照合を開始しました"); setTimeout(function () { location.reload(); }, 1500); } else toast(errText(r), true); });
    }
    if (a === "regen-speech" || a === "regen-ass") {
      t.disabled = true;
      api("POST", "/api/v1/segments/" + t.dataset.id + (a === "regen-speech" ? "/speech" : "/ass")).then(function (r) {
        t.disabled = false;
        if (r.ok) { toast(a === "regen-speech" ? "音声の再生成を受け付けました" : "ASSを更新しました"); setTimeout(function () { location.reload(); }, 800); }
        else toast(errText(r), true);
      });
    }
    if (a === "retry-failed") {
      api("POST", "/api/v1/speech/retry-failed").then(function (r) { if (r.ok) toast("失敗分 " + r.body.accepted + " 件を受け付けました"); else toast(errText(r), true); });
    }
    if (a === "regen-srt") {
      t.disabled = true;
      api("POST", "/api/v1/segments/" + t.dataset.id + "/srt", {}).then(function (r) {
        t.disabled = false;
        if (r.ok) { toast("コメントSRTを更新しました（" + (r.body.status === "skipped" ? "対象なし" : r.body.cues + "件") + "）"); location.reload(); }
        else toast(errText(r), true);
      });
    }
  });

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches && t.matches('input[data-action="toggle"]')) {
      var row = t.closest("tr[data-id]"), body = {}; body[t.dataset.field] = t.checked;
      streamerWrite(() => api("PATCH", "/api/v1/streamers/" + row.dataset.id, body)).then(function (r) {
        if (!r.ok) { t.checked = !t.checked; toast(errText(r), true); } else toast("保存しました");
      });
    }
  });

  /* ---- 配信者追加 ---- */
  var addForm = document.getElementById("add-form");
  if (addForm) addForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var f = addForm.elements, err = document.getElementById("add-error");
    var body = { platform: f.platform.value, account: f.account.value, display_name: f.display_name.value || null,
      monitoring_enabled: f.monitoring_enabled.checked, recording_enabled: f.recording_enabled.checked, comments_enabled: f.comments_enabled.checked };
    streamerWrite(() => api("POST", "/api/v1/streamers", body)).then(function (r) {
      if (r.ok) { location.reload(); } else { err.textContent = errText(r); }
    });
  });

  /* ---- 設定画面 ---- */
  var settings = document.getElementById("settings");
  if (settings) {
    var version = parseInt(settings.dataset.version, 10);
    var fields = Array.prototype.slice.call(settings.querySelectorAll("[data-key]"));
    var save = document.getElementById("save"), discard = document.getElementById("discard"), status = document.getElementById("save-status");
    function read(el) {
      var t = el.dataset.type;
      if (t === "bool") return el.checked;
      if (t === "number") { return el.value === "" ? null : Number(el.value); }
      return el.value;
    }
    function write(el, v) { if (el.dataset.type === "bool") el.checked = !!v; else el.value = v === null ? "" : v; }
    var initial = {}; fields.forEach(function (el) { initial[el.dataset.key] = read(el); });
    function dirtyKeys() { return fields.filter(function (el) { return read(el) !== initial[el.dataset.key]; }); }
    function refresh() {
      var d = dirtyKeys();
      save.disabled = discard.disabled = d.length === 0;
      status.textContent = d.length ? "未保存の変更 " + d.length + " 件" : "変更はありません";
      document.querySelectorAll("[data-dirty-badge]").forEach(function (b) {
        var sec = document.getElementById("cat-" + b.dataset.dirtyBadge);
        b.hidden = !(sec && d.some(function (el) { return sec.contains(el); }));
      });
      var font = settings.querySelector('[data-key="display_font"]');
      document.getElementById("font-preview").style.fontFamily = font.value === "noto-sans-jp" ? '"Noto Sans JP", sans-serif' : '"Segoe UI Variable","Segoe UI","Yu Gothic UI",sans-serif';
      document.getElementById("font-preview").style.fontFeatureSettings = font.value === "noto-sans-jp" ? '"palt" 1' : "normal";
    }
    settings.addEventListener("input", refresh); settings.addEventListener("change", refresh);
    discard.addEventListener("click", function () { fields.forEach(function (el) { write(el, initial[el.dataset.key]); el.removeAttribute("aria-invalid"); }); refresh(); });
    save.addEventListener("click", function () {
      var changed = {}; dirtyKeys().forEach(function (el) { changed[el.dataset.key] = read(el); });
      fields.forEach(function (el) { el.removeAttribute("aria-invalid"); });
      save.disabled = true; status.textContent = "保存中…";
      api("PUT", "/api/v1/settings", { settings: changed, version: version }).then(function (r) {
        if (r.ok) {
          version = r.body.version; settings.dataset.version = version;
          Object.keys(changed).forEach(function (k) { initial[k] = changed[k]; });
          refresh(); toast("設定を保存しました");
          if (changed.display_font || changed.display_timezone) location.reload();
          else if (changed.web_port !== undefined) status.textContent = "保存済み（ポートは再起動後に反映）";
        } else {
          var fl = (r.body.error || {}).fields || {};
          Object.keys(fl).forEach(function (k) { var el = settings.querySelector('[data-key="' + k + '"]'); if (el) el.setAttribute("aria-invalid", "true"); });
          status.textContent = r.status === 409 ? "他で更新されています。再読込してください" : errText(r);
          toast(errText(r), true); save.disabled = false;
        }
      });
    });
    document.querySelectorAll(".c-catnav a").forEach(function (a) {
      a.addEventListener("click", function () { document.querySelectorAll(".c-catnav a").forEach(function (x) { x.removeAttribute("aria-current"); }); a.setAttribute("aria-current", "true"); });
    });
    window.addEventListener("beforeunload", function (e) { if (dirtyKeys().length) { e.preventDefault(); e.returnValue = ""; } });
    refresh();
  }

  /* ---- 配信者一覧の自動更新 ---- */
  var streamerSearch = document.getElementById("streamer-search");
  if (streamerSearch) {
    var pointerDown = false, deferred = false, refreshing = false;

    function refreshBlocked() {
      var focus = document.activeElement;
      var selection = document.getSelection();
      return document.hidden || pointerDown || streamerWrites || document.querySelector("dialog[open]") ||
        streamerSearch.contains(focus) || document.getElementById("streamer-results").contains(focus) ||
        (selection && !selection.isCollapsed);
    }

    function resumeRefresh() {
      requestAnimationFrame(function () {
        if (deferred && !refreshing && !refreshBlocked()) {
          htmx.trigger("#streamer-results", "streamers-resume");
        }
      });
    }

    function interactionChanged() {
      if (document.hidden || (refreshing && refreshBlocked())) deferred = true;
      if (deferred) resumeRefresh();
    }

    document.addEventListener("pointerdown", function () { pointerDown = true; interactionChanged(); });
    ["pointerup", "pointercancel"].forEach(function (event) {
      window.addEventListener(event, function () { pointerDown = false; interactionChanged(); });
    });
    window.addEventListener("blur", function () { pointerDown = false; interactionChanged(); });
    ["focusin", "focusout", "close", "selectionchange", "visibilitychange", "streamer-write"].forEach(function (event) {
      document.addEventListener(event, interactionChanged, true);
    });

    document.body.addEventListener("htmx:beforeRequest", function (e) {
      if (e.detail.target.id !== "streamer-results") return;
      if (refreshBlocked()) {
        deferred = true;
        e.preventDefault();
        return;
      }
      deferred = false;
      refreshing = true;
    });
    document.body.addEventListener("htmx:beforeSwap", function (e) {
      if (e.detail.target.id !== "streamer-results") return;
      if (deferred || refreshBlocked()) {
        deferred = true;
        e.preventDefault();
      }
    });
    document.body.addEventListener("htmx:afterRequest", function (e) {
      if (e.detail.target.id !== "streamer-results") return;
      refreshing = false;
      if (deferred) resumeRefresh();
      else if (e.detail.failed) document.getElementById("streamer-refresh-error").textContent = "更新に失敗しました";
    });
  }

  /* ---- ダッシュボードの自動更新 ---- */
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var a = document.activeElement;
    if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName) && e.detail.target && e.detail.target.contains(a)) e.preventDefault();
    if (document.hidden) e.preventDefault();
  });
})();
