"use strict";
// Sélecteur du « top 3 personnages » du profil : recherche AniList ou parcours d'un
// de ses animes, puis clic pour placer dans un emplacement.
(function () {
  const input = document.getElementById("charSearchInput");
  const btn = document.getElementById("charSearchBtn");
  const results = document.getElementById("charResults");
  const animePicker = document.getElementById("charAnimePicker");
  const form = document.getElementById("charTopForm");
  if (!form) return;

  const slots = [...form.querySelectorAll(".char-slot")];

  function slotHidden(slot) { return slot.querySelector("input[type=hidden]"); }

  function renderSlot(slot, id, name, image) {
    slotHidden(slot).value = id || "";
    const view = slot.querySelector(".char-slot-view");
    const clear = slot.querySelector(".js-char-clear");
    if (id) {
      view.innerHTML = (image ? '<img alt="">' : "") + '<span class="cs-name"></span>';
      if (image) view.querySelector("img").src = image;
      view.querySelector(".cs-name").textContent = name;
      if (clear) clear.hidden = false;
    } else {
      view.innerHTML = '<span class="cs-name">#' + slot.dataset.slot + " — vide</span>";
      if (clear) clear.hidden = true;
    }
  }

  function firstEmptySlot() {
    return slots.find((s) => !slotHidden(s).value) || null;
  }

  function alreadyChosen(id) {
    return slots.some((s) => slotHidden(s).value === String(id));
  }

  function renderResults(items) {
    if (!items.length) {
      results.innerHTML = '<p class="muted small">Aucun personnage trouvé.</p>';
      return;
    }
    results.innerHTML = "";
    for (const it of items) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "char-result";
      el.title = it.name;
      el.innerHTML = '<img alt=""><span></span>';
      el.querySelector("img").src = it.image;
      el.querySelector("span").textContent = it.name;
      el.addEventListener("click", () => {
        if (alreadyChosen(it.id)) return;
        const slot = firstEmptySlot();
        if (!slot) { alert("Les 3 emplacements sont déjà remplis. Retire-en un d'abord."); return; }
        renderSlot(slot, it.id, it.name, it.image);
      });
      results.appendChild(el);
    }
  }

  async function loadResults(url) {
    results.innerHTML = '<p class="muted small">Chargement…</p>';
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      renderResults(data.items || []);
    } catch (e) {
      results.innerHTML = '<p class="muted small">Indisponible, réessaie.</p>';
    }
  }

  function doSearch() {
    const q = (input.value || "").trim();
    if (!q) { results.innerHTML = ""; return; }
    if (animePicker) animePicker.value = "";
    loadResults("/api/characters/search?q=" + encodeURIComponent(q));
  }

  btn?.addEventListener("click", doSearch);
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });
  animePicker?.addEventListener("change", () => {
    const id = animePicker.value;
    if (!id) { results.innerHTML = ""; return; }
    if (input) input.value = "";
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
  document.getElementById("charAnimeNext")?.addEventListener("click", () => stepAnime(1));
  document.getElementById("charAnimePrev")?.addEventListener("click", () => stepAnime(-1));

  form.addEventListener("click", (e) => {
    const clr = e.target.closest(".js-char-clear");
    if (!clr) return;
    const slot = slots.find((s) => s.dataset.slot === clr.dataset.slot);
    if (slot) renderSlot(slot, "", "", "");
  });
})();
