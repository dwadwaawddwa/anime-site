"use strict";
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";

// --- Notes en 1 clic ---------------------------------------------------------
function paintRating(widget, val) {
  widget.querySelectorAll(".star").forEach((star, i) => {
    const full = i + 1, half = i + 0.5;
    star.classList.toggle("on", val >= half);
    star.classList.toggle("full", val >= full);
  });
  const num = widget.querySelector(".rating-num");
  if (num) num.textContent = val ? String(val).replace(/\.0$/, "") : "—";
}

async function sendRating(entryId, val) {
  const body = new URLSearchParams({ entry_id: entryId, rating: String(val), csrf: CSRF });
  const r = await fetch("/entry/rate", { method: "POST", body,
    headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  if (!r.ok) { alert("Impossible d'enregistrer la note."); return null; }
  return r.json();
}

document.addEventListener("click", async (e) => {
  const star = e.target.closest(".star");
  if (star) {
    const widget = star.closest(".rating");
    const val = parseFloat((e.target.closest("[data-val]") || star).dataset.val);
    const prev = widget.dataset.val;
    paintRating(widget, val);
    const res = await sendRating(widget.dataset.entry, val);
    if (res) { widget.dataset.val = res.rating || 0; }
    return;
  }
  const clr = e.target.closest(".rating .clear");
  if (clr) {
    const widget = clr.closest(".rating");
    paintRating(widget, 0);
    await sendRating(widget.dataset.entry, 0);
    clr.remove();
    return;
  }
  const tc = e.target.closest(".toggle-comment");
  if (tc) {
    const el = document.getElementById(tc.dataset.target);
    if (el) el.hidden = !el.hidden;
  }
});

// Aperçu de la note au survol
document.addEventListener("mouseover", (e) => {
  const star = e.target.closest(".star");
  if (!star) return;
  const widget = star.closest(".rating");
  const val = parseFloat((e.target.closest("[data-val]") || star).dataset.val);
  widget.querySelectorAll(".star").forEach((s, i) => {
    s.style.setProperty("--preview", (i + 1) <= val ? "1" : "0");
  });
});

// --- Auto-submit & confirmations (la CSP interdit les handlers inline) -------
// Un <select>/<input> avec .js-submit soumet son formulaire quand il change.
document.addEventListener("change", (e) => {
  const el = e.target.closest(".js-submit");
  if (el && el.form) el.form.submit();
});
// Un <form data-confirm="…"> demande confirmation avant d'être soumis.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset ? e.target.dataset.confirm : null;
  if (msg && !confirm(msg)) e.preventDefault();
});

// --- Mode édition de la tier list -------------------------------------------
const editToggle = document.getElementById("editToggle");
if (editToggle) {
  editToggle.addEventListener("click", () => {
    document.body.classList.toggle("editing");
    const on = document.body.classList.contains("editing");
    editToggle.textContent = on ? "✓ Terminé" : "✎ Éditer";
    document.querySelector(".addtier")?.toggleAttribute("hidden", !on);
  });
}
