// Heatmap d'activité : clic sur une case -> détail des épisodes du jour.
// (Chargé sur /profile et /u/<id>. CSP: script-src 'self', fetch same-origin.)
(function () {
  "use strict";

  var SRC_LABEL = {
    crunchyroll: "Crunchyroll",
    "anime-sama": "anime-sama",
    manual: "manuel",
    extension: "extension",
  };

  function fmtDate(iso) {
    try {
      var d = new Date(iso + "T00:00:00");
      return d.toLocaleDateString("fr-FR", {
        weekday: "long", day: "numeric", month: "long", year: "numeric",
      });
    } catch (e) { return iso; }
  }

  function initGrid(wrap) {
    var grid = wrap.querySelector(".hm");
    var detail = wrap.parentNode.querySelector(".hm-detail");
    if (!grid || !detail) return;
    var user = wrap.getAttribute("data-user") || "";
    var dateEl = detail.querySelector(".hm-detail-date");
    var listEl = detail.querySelector(".hm-detail-list");
    var closeBtn = detail.querySelector(".hm-detail-close");
    var current = null;

    function clearActive() {
      var a = grid.querySelector(".hm-cell.active");
      if (a) a.classList.remove("active");
    }

    function open(cell) {
      var date = cell.getAttribute("data-date");
      if (!date) return;
      clearActive();
      cell.classList.add("active");
      current = date;
      dateEl.textContent = fmtDate(date);
      listEl.innerHTML = '<li class="muted">Chargement…</li>';
      detail.hidden = false;

      var url = "/profile/activity/day?date=" + encodeURIComponent(date);
      if (user) url += "&user=" + encodeURIComponent(user);
      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          if (current !== date) return; // une autre case a été cliquée entre-temps
          if (!data.items || !data.items.length) {
            listEl.innerHTML = '<li class="muted">Rien ce jour-là.</li>';
            return;
          }
          listEl.innerHTML = "";
          data.items.forEach(function (it) {
            var li = document.createElement("li");
            var title = it.title || "?";
            var parts = [];
            if (it.episode != null) parts.push("ép. " + it.episode);
            var src = SRC_LABEL[it.source] || it.source;
            if (src) parts.push(src);
            if (it.time) parts.push(it.time);
            var meta = parts.length ? " — " + parts.join(" · ") : "";
            if (it.anime_id) {
              var a = document.createElement("a");
              a.href = "/anime/" + it.anime_id;
              a.textContent = title;
              li.appendChild(a);
              li.appendChild(document.createTextNode(meta));
            } else {
              li.textContent = title + meta;
            }
            listEl.appendChild(li);
          });
        })
        .catch(function () {
          listEl.innerHTML = '<li class="err-inline">Erreur de chargement.</li>';
        });
    }

    grid.addEventListener("click", function (e) {
      var cell = e.target.closest(".hm-cell[data-count]");
      if (cell && cell.getAttribute("data-count") !== "0") open(cell);
    });
    grid.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var cell = e.target.closest(".hm-cell[data-count]");
      if (cell && cell.getAttribute("data-count") !== "0") { e.preventDefault(); open(cell); }
    });
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        detail.hidden = true; current = null; clearActive();
      });
    }
  }

  function initCopy() {
    var btn = document.querySelector(".js-copy-token");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var field = document.querySelector(".token-field");
      if (!field) return;
      field.select();
      var done = function () {
        var old = btn.textContent;
        btn.textContent = "Copié ✓";
        setTimeout(function () { btn.textContent = old; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(field.value).then(done, done);
      } else {
        try { document.execCommand("copy"); } catch (e) {}
        done();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".hm-grid-wrap").forEach(initGrid);
    initCopy();
  });
})();
