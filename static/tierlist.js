"use strict";
// Tier list : drag & drop avec ligne d'insertion, ordre persistant, export PNG, partage.
// Gère deux types de listes : animes (data-entry, /tier/place) et personnages
// (data-char, /char/place + recherche/ajout/retrait de personnages).
(function () {
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const root = document.getElementById("tierlist");
  const BOARD_ID = root ? Number(root.dataset.board || 0) : 0;
  const KIND = root ? (root.dataset.kind || "anime") : "anime";
  const IS_CHAR = KIND === "character";

  const itemId = (el) => Number(el.dataset.entry || el.dataset.char || 0);

  // ---- Bouton « ＋ Nouvelle » (révèle le champ nom) ----
  document.getElementById("newBoardBtn")?.addEventListener("click", () => {
    const f = document.getElementById("newBoardForm");
    if (f) { f.hidden = !f.hidden; if (!f.hidden) f.querySelector("input[name=name]")?.focus(); }
  });

  // ---- Ligne d'insertion (indicateur de position) ----
  const line = document.createElement("div");
  line.className = "drop-line";

  function clearLine() { line.remove(); }

  function placeLine(dropEl, x, y) {
    const before = insertBefore(dropEl, x, y);
    if (before) dropEl.insertBefore(line, before);
    else dropEl.appendChild(line);
  }

  // Élément AVANT lequel insérer, selon la position du curseur (gère le retour à la ligne).
  function insertBefore(dropEl, x, y) {
    const items = [...dropEl.querySelectorAll(".tier-item")]
      .filter((it) => it !== dragged && it !== line);
    let best = null, bestDist = Infinity;
    for (const it of items) {
      const r = it.getBoundingClientRect();
      if (y >= r.top - 10 && y <= r.bottom + 10) {        // même ligne visuelle
        const cx = r.left + r.width / 2;
        if (x < cx && cx - x < bestDist) { bestDist = cx - x; best = it; }
      }
    }
    return best;
  }

  async function commit(id, tierId, dropEl, itemEl) {
    if (line.parentNode) dropEl.insertBefore(itemEl, line);
    else dropEl.appendChild(itemEl);
    clearLine();
    const order = [...dropEl.querySelectorAll(".tier-item")].map(itemId);
    const payload = { tier_id: tierId, order, board_id: BOARD_ID, csrf: CSRF };
    if (IS_CHAR) payload.char_id = Number(id);
    else payload.entry_id = Number(id);
    const r = await fetch(IS_CHAR ? "/char/place" : "/tier/place", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) { alert("Déplacement refusé."); location.reload(); }
  }

  let dragged = null;

  if (root) {
    // ---- Souris (HTML5 drag) ----
    root.addEventListener("dragstart", (e) => {
      const item = e.target.closest(".tier-item");
      if (!item) return;
      dragged = item; item.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    root.addEventListener("dragend", () => {
      if (dragged) dragged.classList.remove("dragging");
      dragged = null; clearLine();
      root.querySelectorAll(".dragover").forEach((d) => d.classList.remove("dragover"));
    });
    root.addEventListener("dragover", (e) => {
      const drop = e.target.closest(".tier-drop");
      if (!drop || !dragged) return;
      e.preventDefault();
      root.querySelectorAll(".dragover").forEach((d) => d.classList.remove("dragover"));
      drop.classList.add("dragover");
      placeLine(drop, e.clientX, e.clientY);
    });
    root.addEventListener("drop", (e) => {
      const drop = e.target.closest(".tier-drop");
      if (!drop || !dragged) return;
      e.preventDefault();
      drop.classList.remove("dragover");
      const tid = drop.dataset.tier === "unranked" ? "unranked" : Number(drop.dataset.tier);
      commit(itemId(dragged), tid, drop, dragged);
    });

    // ---- Tactile (mobile) ----
    let touchItem = null, ghost = null;
    root.addEventListener("touchstart", (e) => {
      const item = e.target.closest(".tier-item");
      if (!item) return;
      touchItem = dragged = item;
      ghost = item.cloneNode(true);
      Object.assign(ghost.style, { position: "fixed", pointerEvents: "none", opacity: ".85", zIndex: 999, width: "52px" });
      document.body.appendChild(ghost);
      item.classList.add("dragging");
    }, { passive: true });
    root.addEventListener("touchmove", (e) => {
      if (!touchItem) return;
      const t = e.touches[0];
      ghost.style.left = (t.clientX - 26) + "px";
      ghost.style.top = (t.clientY - 37) + "px";
      root.querySelectorAll(".dragover").forEach((d) => d.classList.remove("dragover"));
      const drop = document.elementFromPoint(t.clientX, t.clientY)?.closest(".tier-drop");
      if (drop) { drop.classList.add("dragover"); placeLine(drop, t.clientX, t.clientY); }
      e.preventDefault();
    }, { passive: false });
    root.addEventListener("touchend", (e) => {
      if (!touchItem) return;
      const t = e.changedTouches[0];
      const drop = document.elementFromPoint(t.clientX, t.clientY)?.closest(".tier-drop");
      if (drop) {
        const tid = drop.dataset.tier === "unranked" ? "unranked" : Number(drop.dataset.tier);
        commit(itemId(touchItem), tid, drop, touchItem);
      } else { clearLine(); }
      touchItem.classList.remove("dragging");
      ghost?.remove(); ghost = null; touchItem = dragged = null;
      root.querySelectorAll(".dragover").forEach((d) => d.classList.remove("dragover"));
    });
  }

  // ---- Personnages : recherche / parcours par anime + ajout au board ----
  const addInput = document.getElementById("charAddInput");
  const addBtn = document.getElementById("charAddBtn");
  const addResults = document.getElementById("charAddResults");
  const animePicker = document.getElementById("animePicker");

  async function postForm(url, data) {
    const body = new URLSearchParams(data);
    return fetch(url, { method: "POST", body,
      headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  }

  // Insère un perso dans « À classer » sans recharger (pour enchaîner les ajouts).
  function addToUnranked(it) {
    if (!root) return;
    if (root.querySelector('.tier-item[data-char="' + it.id + '"]')) return; // déjà présent
    const drop = root.querySelector('.tier-drop[data-tier="unranked"]');
    if (!drop) return;
    const item = document.createElement("div");
    item.className = "tier-item";
    item.draggable = true;
    item.dataset.char = it.id;
    item.title = it.name;
    item.innerHTML = '<img alt=""><span class="ti-title"></span>' +
      '<button type="button" class="ti-del js-char-del" title="Retirer de la liste">✕</button>';
    item.querySelector("img").src = it.image;
    item.querySelector(".ti-title").textContent = it.name;
    item.querySelector(".ti-del").dataset.char = it.id;
    drop.appendChild(item);
  }

  function renderResults(items) {
    if (!items.length) { addResults.innerHTML = '<p class="muted small">Aucun personnage trouvé.</p>'; return; }
    addResults.innerHTML = "";
    for (const it of items) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "char-result";
      el.title = it.name;
      el.innerHTML = '<img alt=""><span></span>';
      el.querySelector("img").src = it.image;
      el.querySelector("span").textContent = it.name;
      // Déjà sur le board ? on le marque d'emblée comme ajouté.
      if (root && root.querySelector('.tier-item[data-char="' + it.id + '"]')) {
        el.classList.add("added");
      }
      el.addEventListener("click", async () => {
        if (el.classList.contains("added")) return;
        el.disabled = true;
        try {
          const r = await postForm("/char/add", { board_id: BOARD_ID, char_id: it.id, csrf: CSRF });
          if (!r.ok) throw new Error(r.status);
          const data = await r.json();
          addToUnranked(data.char || it);
          el.classList.add("added");  // reste visible, la liste ne se ferme pas
        } catch (err) {
          alert("Ajout impossible.");
          el.disabled = false;
        }
      });
      addResults.appendChild(el);
    }
  }

  async function loadResults(url) {
    addResults.innerHTML = '<p class="muted small">Chargement…</p>';
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      renderResults(data.items || []);
    } catch (e) {
      addResults.innerHTML = '<p class="muted small">Indisponible, réessaie.</p>';
    }
  }

  function searchChars() {
    const q = (addInput.value || "").trim();
    if (!q) { addResults.innerHTML = ""; return; }
    if (animePicker) animePicker.value = "";
    loadResults("/api/characters/search?q=" + encodeURIComponent(q));
  }

  addBtn?.addEventListener("click", searchChars);
  addInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); searchChars(); }
  });
  animePicker?.addEventListener("change", () => {
    const id = animePicker.value;
    if (!id) { addResults.innerHTML = ""; return; }
    if (addInput) addInput.value = "";
    loadResults("/api/anime/" + encodeURIComponent(id) + "/characters");
  });

  // Boutons « anime précédent / suivant » : défilent la liste sans rouvrir le menu.
  function stepAnime(delta) {
    if (!animePicker || animePicker.options.length <= 1) return;
    const n = animePicker.options.length;        // option 0 = placeholder
    let i = animePicker.selectedIndex;
    if (i < 1) i = (delta > 0 ? 0 : n);          // depuis le placeholder
    i += delta;
    if (i < 1) i = n - 1;                         // boucle vers le dernier
    if (i > n - 1) i = 1;                         // boucle vers le premier
    animePicker.selectedIndex = i;
    animePicker.dispatchEvent(new Event("change"));
  }
  document.getElementById("animeNext")?.addEventListener("click", () => stepAnime(1));
  document.getElementById("animePrev")?.addEventListener("click", () => stepAnime(-1));

  // ---- Personnages : retrait d'un perso du board (bouton ✕ en mode édition) ----
  root?.addEventListener("click", async (e) => {
    const del = e.target.closest(".js-char-del");
    if (!del) return;
    e.preventDefault();
    if (!document.body.classList.contains("editing")) return;
    if (!confirm("Retirer ce personnage de la liste ?")) return;
    const r = await postForm("/char/remove", { board_id: BOARD_ID, char_id: del.dataset.char, csrf: CSRF });
    if (r.ok) location.reload(); else alert("Retrait impossible.");
  });

  // ---- Partage ----
  document.getElementById("shareBtn")?.addEventListener("click", async (e) => {
    const b = e.currentTarget;
    const url = location.origin + b.dataset.url;
    if (b.dataset.public !== "1" &&
        !confirm("Ton profil est privé : ce lien ne marchera que si tu passes ton profil en public (page Profil). Copier quand même ?"))
      return;
    try { await navigator.clipboard.writeText(url); b.textContent = "✅ Lien copié !"; }
    catch { prompt("Copie ce lien :", url); }
    setTimeout(() => (b.textContent = "🔗 Partager"), 1600);
  });

  // ---- Export PNG (html2canvas auto-hébergé, images via proxy same-origin) ----
  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = src; s.onload = res; s.onerror = rej; document.head.appendChild(s);
    });
  }
  function waitImages(el) {
    return Promise.all([...el.querySelectorAll("img")].map((img) =>
      img.complete ? Promise.resolve() : new Promise((r) => { img.onload = img.onerror = r; })));
  }
  document.getElementById("exportPng")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; const old = btn.textContent; btn.textContent = "⏳ Génération…";
    try {
      if (!window.html2canvas) await loadScript("/static/html2canvas.min.js");
      document.body.classList.remove("editing");
      const src = document.getElementById("tierlist");
      const clone = src.cloneNode(true);
      clone.classList.add("exporting");
      clone.querySelectorAll("img").forEach((img) => {
        const s = img.getAttribute("src") || "";
        if (/^https:\/\/(media\.kitsu\.(app|io)|s4\.anilist\.co)\//.test(s)) img.src = "/img?u=" + encodeURIComponent(s);
      });
      const wrap = document.createElement("div");
      wrap.style.cssText = "position:fixed;left:-99999px;top:0;width:" + src.offsetWidth +
        "px;background:#050506;padding:18px;border-radius:12px";
      wrap.appendChild(clone); document.body.appendChild(wrap);
      await waitImages(clone);
      const canvas = await html2canvas(wrap, { backgroundColor: "#050506", useCORS: true, scale: 2, logging: false });
      wrap.remove();
      canvas.toBlob((b) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(b); a.download = "tierlist-" + KIND + ".png"; a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      }, "image/png");
    } catch (err) { alert("Export impossible : " + err); }
    finally { btn.disabled = false; btn.textContent = old; }
  });
})();
