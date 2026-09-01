// First-run prerequisite screen. Self-contained: no dependency on app.js.
// Shows a blocking dialog when GET /api/preflight reports a machine that is not
// ready, walks the user through each install action, and closes when ready.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var mb = function (n) {
    if (n == null) return "";
    return n >= 1024 ? (n / 1024).toFixed(1) + " GB" : Math.round(n) + " MB";
  };

  var ICON = { ok: "✓", missing: "•", insufficient: "!", unknown: "?" };

  function profileLine(p) {
    var bits = [];
    if (p.os) bits.push(p.os + (p.arch ? " " + p.arch : ""));
    if (p.ram_gb) bits.push(p.ram_gb + " GB RAM");
    if (p.gpu_vendor) {
      var name = p.gpu_vendor === "nvidia" ? "NVIDIA"
        : p.gpu_vendor === "amd" ? "AMD"
        : p.gpu_vendor === "apple" ? "Apple" : "Intel";
      // AdapterRAM is unreliable for AMD/Intel; only trust an NVIDIA/Apple VRAM figure.
      var showVram = p.vram_gb && (p.gpu_vendor === "nvidia" || p.gpu_vendor === "apple");
      bits.push(name + (showVram ? " " + p.vram_gb + " GB VRAM" : " GPU"));
    } else {
      bits.push("no dedicated GPU");
    }
    if (p.disk_free_gb != null) bits.push(p.disk_free_gb + " GB free");
    return bits.join("  ·  ");
  }

  function render(report) {
    var p = report.profile;
    var rows = report.requirements.map(function (r) {
      var action = "";
      if (r.action) {
        var size = r.download_mb
          ? ' <span class="pf-size">' + mb(r.download_mb) + "</span>" : "";
        action =
          '<div class="pf-action" data-action="' + esc(r.action) + '">' +
            '<button class="primary pf-run">' +
              (r.action === "install_ollama" ? "Install" : "Download") + size +
            "</button>" +
            '<div class="pf-progress" hidden>' +
              '<div class="pf-bar"><span></span></div>' +
              '<small class="pf-status"></small>' +
            "</div>" +
          "</div>";
      }
      return (
        '<li class="pf-req pf-' + esc(r.status) + '">' +
          '<span class="pf-mark">' + (ICON[r.status] || "•") + "</span>" +
          "<div><strong>" + esc(r.label) + "</strong>" +
            "<small>" + esc(r.detail) + "</small></div>" +
          action +
        "</li>"
      );
    }).join("");

    var notes = (report.notes || []).map(function (n) {
      return '<p class="pf-note">' + esc(n) + "</p>";
    }).join("");

    var footer;
    if (report.ready) {
      footer = '<button class="primary large" id="pfContinue">' +
        (report.degraded ? "Continue anyway" : "Continue") + "</button>";
    } else {
      footer = '<button class="ghost" id="pfRecheck">Re-check</button>' +
        '<span class="pf-hint">Complete the required steps above to continue.</span>';
    }

    $("preflightBody").innerHTML =
      '<p class="pf-machine">' + esc(profileLine(p)) + "</p>" +
      (report.recommended_model
        ? '<p class="pf-rec">Recommended model: <code>' + esc(report.recommended_model) +
          "</code> — " + esc(report.model_reason) + "</p>"
        : "") +
      '<ul class="pf-list">' + rows + "</ul>" +
      notes +
      '<div class="pf-footer">' + footer + "</div>";

    Array.prototype.forEach.call(document.querySelectorAll(".pf-action"), function (el) {
      el.querySelector(".pf-run").addEventListener("click", function () {
        runAction(el.dataset.action, el);
      });
    });
    var cont = $("pfContinue");
    if (cont) cont.addEventListener("click", function () {
      if (report.degraded) {
        try { localStorage.setItem("ultron.preflight.ack", "1"); } catch (e) { /* private mode */ }
      }
      $("preflightDialog").close();
    });
    var recheck = $("pfRecheck");
    if (recheck) recheck.addEventListener("click", load);
  }

  function applyFrame(evt, bar, status) {
    var done = evt.completed != null ? evt.completed : evt.downloaded;
    if (evt.total && done != null) {
      bar.style.width = Math.min(100, (done / evt.total) * 100).toFixed(1) + "%";
    }
    status.textContent = evt.status || evt.phase || "";
  }

  function runAction(action, el) {
    var btn = el.querySelector(".pf-run");
    var prog = el.querySelector(".pf-progress");
    var bar = el.querySelector(".pf-bar span");
    var status = el.querySelector(".pf-status");
    btn.disabled = true;
    prog.hidden = false;
    status.textContent = "starting…";

    fetch("/api/preflight/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action }),
    }).then(function (resp) {
      var reader = resp.body.getReader();
      var dec = new TextDecoder();
      var buf = "";

      function finish(ok, err) {
        if (ok) {
          status.textContent = "done";
          setTimeout(load, 400);
        } else {
          status.textContent = "failed: " + (err || "unknown error");
          btn.disabled = false;
        }
      }

      function pump() {
        return reader.read().then(function (chunk) {
          buf += dec.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(function (frame) {
            var line = frame.split("\n").filter(function (l) {
              return l.indexOf("data: ") === 0;
            })[0];
            if (!line) return;
            var evt;
            try { evt = JSON.parse(line.slice(6)); } catch (e) { return; }
            applyFrame(evt, bar, status);
            if (evt.phase === "done") finish(true);
            else if (evt.phase === "error") finish(false, evt.error);
          });
          if (!chunk.done) return pump();
        });
      }

      return pump();
    }).catch(function (e) {
      status.textContent = "failed: " + e.message;
      btn.disabled = false;
    });
  }

  function load() {
    return fetch("/api/preflight")
      .then(function (r) { return r.json(); })
      .then(function (report) {
        render(report);
        var dlg = $("preflightDialog");
        var acked = false;
        try { acked = localStorage.getItem("ultron.preflight.ack") === "1"; } catch (e) { /* */ }
        var mustShow = !report.ready || (report.degraded && !acked);
        if (mustShow) {
          if (!dlg.open) dlg.showModal();
        } else if (dlg.open) {
          dlg.close();
        }
      })
      .catch(function () { /* offline or endpoint missing: never block the app */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
