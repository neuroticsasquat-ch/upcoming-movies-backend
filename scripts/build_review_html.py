"""Generate a self-contained HTML page for reviewing the link/cluster validation candidates.

Run in the container:
    task shell
    python scripts/build_review_html.py < tests/fixtures/link/validation_candidates.json \
        > tests/fixtures/link/validation_review.html

Then open tests/fixtures/link/validation_review.html in a browser (file://...). Full text,
a searchable film picker (no need to know TMDB ids), dropdowns for relation/event_type, and
a "Download validation_set.json" button. Edits autosave to the browser's localStorage so you
can close and resume. Drop the downloaded file at tests/fixtures/link/validation_set.json."""

import json
import re
import sys

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.db import SessionLocal

EVENT_TYPES = [
    "announced",
    "casting",
    "production_start",
    "production_wrap",
    "release_date",
    "trailer",
    "other",
]
NONE_SAMPLE_TARGET = 90


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


async def _films():
    async with SessionLocal() as s:
        films = (await s.execute(select(Film))).scalars().all()
    label_by_id, roster, multiword, singleword = {}, [], set(), set()
    for f in films:
        year = f.release_date.year if f.release_date else None
        label = f"{f.title} ({year})" if year else f.title
        label_by_id[f.tmdb_id] = label
        roster.append({"id": f.tmdb_id, "label": label})
        for t in (f.title, f.original_title):
            n = _norm(t or "")
            if len(n.split()) >= 2 and len(n) > 3:
                multiword.add(n)
            elif len(n.split()) == 1 and len(n) >= 5:
                singleword.add(n)
    # de-dupe labels so the picker maps each label to exactly one id
    seen = {}
    for r in roster:
        if r["label"] in seen:
            r["label"] = f"{r['label']} ·#{r['id']}"
        seen[r["label"]] = r["id"]
        label_by_id[r["id"]] = r["label"]
    roster.sort(key=lambda r: r["label"].lower())
    return label_by_id, roster, multiword, singleword


def _flagged(title: str, multiword: set[str], singleword: set[str]) -> bool:
    n = _norm(title)
    if any(m in n for m in multiword):
        return True
    return any(w in set(n.split()) for w in singleword)


async def main() -> None:
    rows = json.load(sys.stdin)
    label_by_id, roster, multiword, singleword = await _films()

    cand = []
    for r in rows:
        rel = r["relation"]
        flagged = rel == "none" and _flagged(r["title"], multiword, singleword)
        tmdb = r["expected_film_tmdb_id"]
        cand.append(
            {
                "url": r["url"],
                "source": r["source"],
                "title": r["title"],
                "summary": r.get("summary", ""),
                "relation": rel,
                "tmdb_id": tmdb,
                "film_label": label_by_id.get(tmdb, "") if rel == "about" else "",
                "event_type": r["event_type"],
                "flagged": flagged,
            }
        )
    # default keep: positives + flagged + a deterministic stride sample of plain nones
    plain = [c for c in cand if c["relation"] == "none" and not c["flagged"]]
    n_flag = sum(1 for c in cand if c["flagged"])
    need = max(0, NONE_SAMPLE_TARGET - n_flag)
    stride = max(1, len(plain) // need) if need else len(plain) + 1
    keep_urls = {c["url"] for c in plain[::stride][:need]}
    for c in cand:
        c["keep"] = c["relation"] in ("about", "mention") or c["flagged"] or c["url"] in keep_urls

    data = json.dumps(
        {"candidates": cand, "roster": roster, "eventTypes": EVENT_TYPES}, ensure_ascii=False
    ).replace("</", "<\\/")
    html = _TEMPLATE.replace("/*__DATA__*/null", data)
    sys.stdout.buffer.write(html.encode("utf-8"))
    print(f"{len(cand)} rows, {len(roster)} films, {n_flag} flagged nones", file=sys.stderr)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link validation review</title>
<style>
  :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { margin: 0; background: #f4f5f7; color: #1a1a1a; }
  header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd;
    padding: 10px 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; z-index: 10; }
  header .stats { font-size: 13px; color: #444; }
  header .stats b { color: #000; }
  header .grow { flex: 1; }
  button, select, input { font-size: 14px; }
  button { padding: 6px 12px; border: 1px solid #bbb; border-radius: 6px; background: #fff; cursor: pointer; }
  button.primary { background: #1f6feb; color: #fff; border-color: #1f6feb; font-weight: 600; }
  button.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
  #search { padding: 6px 10px; border: 1px solid #bbb; border-radius: 6px; min-width: 200px; }
  main { padding: 16px; max-width: 920px; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }
  .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 14px; }
  .card.dropped { opacity: 0.5; }
  .card .top { display: flex; gap: 10px; align-items: center; font-size: 12px; color: #666; margin-bottom: 4px; }
  .badge { padding: 1px 7px; border-radius: 10px; font-weight: 600; font-size: 11px; }
  .badge.about { background: #dcfce7; color: #166534; }
  .badge.mention { background: #fef9c3; color: #854d0e; }
  .badge.none { background: #f1f5f9; color: #475569; }
  .flag { background: #fee2e2; color: #991b1b; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .untracked-badge { background: #ede9fe; color: #6d28d9; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .utlabel { color: #6d28d9; }
  .card h3 { margin: 2px 0 4px; font-size: 15px; line-height: 1.35; }
  .card .summary { font-size: 13px; color: #333; line-height: 1.45; margin: 0 0 8px; }
  .card a.src { color: #1f6feb; text-decoration: none; font-size: 12px; }
  .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; border-top: 1px dashed #eee; padding-top: 8px; }
  .controls label { font-size: 12px; color: #555; display: flex; gap: 4px; align-items: center; }
  .filmwrap { display: flex; gap: 4px; align-items: center; }
  .filmwrap input { padding: 5px 8px; border: 1px solid #bbb; border-radius: 6px; min-width: 280px; }
  .filmwrap input.bad { border-color: #dc2626; background: #fef2f2; }
  .filmwrap input.ok { border-color: #16a34a; }
  .hidden { display: none !important; }
  .saved { font-size: 12px; color: #16a34a; }
</style>
</head>
<body>
<header>
  <strong>Link validation review</strong>
  <span id="filters"></span>
  <input id="search" placeholder="filter by headline…" autocomplete="off">
  <span class="grow"></span>
  <span class="stats" id="stats"></span>
  <span class="saved" id="saved"></span>
  <button id="reset">Reset edits</button>
  <button class="primary" id="download">Download validation_set.json</button>
</header>
<main id="list"></main>
<datalist id="films"></datalist>
<script>
const DATA = /*__DATA__*/null;
const STORE_KEY = "upmovies-link-review-v1";
const { candidates, roster, eventTypes } = DATA;
const labelToId = {};
roster.forEach(r => labelToId[r.label] = r.id);

// hydrate state from localStorage (keyed by url) so edits survive reloads
let saved = {};
try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) {}
const state = candidates.map(c => {
  const s = saved[c.url] || {};
  return {
    url: c.url, source: c.source, title: c.title, summary: c.summary, flagged: c.flagged,
    keep: s.keep !== undefined ? s.keep : c.keep,
    relation: s.relation !== undefined ? s.relation : c.relation,
    tmdb_id: s.tmdb_id !== undefined ? s.tmdb_id : c.tmdb_id,
    film_label: s.film_label !== undefined ? s.film_label : c.film_label,
    event_type: s.event_type !== undefined ? s.event_type : c.event_type,
    event_group: s.event_group !== undefined ? s.event_group : "",
    untracked: s.untracked !== undefined ? s.untracked : false,
  };
});

const filmsDl = document.getElementById("films");
roster.forEach(r => { const o = document.createElement("option"); o.value = r.label; filmsDl.appendChild(o); });

let filter = "about";
const FILTERS = [
  ["about", r => r.relation === "about"],
  ["mention", r => r.relation === "mention"],
  ["flagged", r => r.flagged],
  ["untracked", r => r.untracked],
  ["kept", r => r.keep],
  ["all", () => true],
];
let search = "";

function persist() {
  const out = {};
  state.forEach(r => out[r.url] = {
    keep: r.keep, relation: r.relation, tmdb_id: r.tmdb_id,
    film_label: r.film_label, event_type: r.event_type, event_group: r.event_group,
    untracked: r.untracked,
  });
  localStorage.setItem(STORE_KEY, JSON.stringify(out));
  const el = document.getElementById("saved"); el.textContent = "saved ✓";
  clearTimeout(persist._t); persist._t = setTimeout(() => el.textContent = "", 1200);
}

function rowInvalid(r) {
  return r.keep && r.relation === "about" &&
    (!r.tmdb_id || !labelToId[r.film_label] || !eventTypes.includes(r.event_type));
}

function refreshStats() {
  const kept = state.filter(r => r.keep);
  const about = kept.filter(r => r.relation === "about").length;
  const invalid = state.filter(rowInvalid).length;
  document.getElementById("stats").innerHTML =
    `kept <b>${kept.length}</b> · about <b>${about}</b> · mention <b>${kept.filter(r=>r.relation==="mention").length}</b> · none <b>${kept.filter(r=>r.relation==="none").length}</b>` +
    (invalid ? ` · <span style="color:#dc2626">${invalid} need fixing</span>` : ` · <span style="color:#16a34a">ready</span>`);
}

function render() {
  const list = document.getElementById("list");
  list.innerHTML = "";
  const f = FILTERS.find(x => x[0] === filter)[1];
  const q = search.toLowerCase();
  state.forEach((r, i) => {
    if (!f(r)) return;
    if (q && !r.title.toLowerCase().includes(q)) return;
    const card = document.createElement("div");
    card.className = "card" + (r.keep ? "" : " dropped");
    const isAbout = r.relation === "about";
    card.innerHTML = `
      <div class="top">
        <span class="badge ${r.relation}">${r.relation}</span>
        ${r.flagged ? '<span class="flag">possible missed link</span>' : ''}
        ${r.untracked ? '<span class="untracked-badge">untracked film</span>' : ''}
        <span>${r.source}</span>
      </div>
      <h3>${escapeHtml(r.title)}</h3>
      <p class="summary">${escapeHtml(r.summary || "")}</p>
      <a class="src" href="${escapeAttr(r.url)}" target="_blank" rel="noopener">open story ↗</a>
      <div class="controls">
        <label><input type="checkbox" class="k" ${r.keep ? "checked" : ""}> keep</label>
        <label>relation
          <select class="rel">
            ${["about","mention","none"].map(o => `<option ${o===r.relation?"selected":""}>${o}</option>`).join("")}
          </select>
        </label>
        <span class="filmwrap ${isAbout ? "" : "hidden"}">film
          <input class="film" list="films" placeholder="search a film…" value="${escapeAttr(r.film_label || "")}">
        </span>
        <label class="etwrap ${isAbout ? "" : "hidden"}">event
          <select class="et">
            <option value="">—</option>
            ${eventTypes.map(o => `<option ${o===r.event_type?"selected":""}>${o}</option>`).join("")}
          </select>
        </label>
        <label>group <input class="eg" value="${escapeAttr(r.event_group || "")}" placeholder="(optional)" style="width:130px"></label>
        <label class="utlabel"><input type="checkbox" class="ut" ${r.untracked ? "checked" : ""}> untracked film</label>
      </div>`;
    const filmInput = card.querySelector(".film");
    markFilm(filmInput, r);
    card.querySelector(".k").onchange = e => { r.keep = e.target.checked; card.classList.toggle("dropped", !r.keep); refreshStats(); persist(); };
    card.querySelector(".rel").onchange = e => { r.relation = e.target.value; render(); persist(); };
    if (filmInput) filmInput.oninput = e => { r.film_label = e.target.value; r.tmdb_id = labelToId[e.target.value] || null; markFilm(e.target, r); refreshStats(); persist(); };
    const et = card.querySelector(".et"); if (et) et.onchange = e => { r.event_type = e.target.value || null; refreshStats(); persist(); };
    card.querySelector(".eg").oninput = e => { r.event_group = e.target.value; persist(); };
    card.querySelector(".ut").onchange = e => { r.untracked = e.target.checked; render(); persist(); };
    list.appendChild(card);
  });
  refreshStats();
  renderFilters();
}

function markFilm(input, r) {
  if (!input) return;
  input.classList.toggle("ok", !!labelToId[input.value]);
  input.classList.toggle("bad", r.relation === "about" && r.keep && !labelToId[input.value]);
}
function escapeHtml(s) { return (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function escapeAttr(s) { return (s||"").replace(/"/g, "&quot;").replace(/</g,"&lt;"); }

function renderFilters() {
  const el = document.getElementById("filters");
  el.innerHTML = FILTERS.map(([name, fn]) =>
    `<button class="fbtn ${name===filter?"active":""}" data-f="${name}">${name} (${state.filter(fn).length})</button>`).join(" ");
  el.querySelectorAll(".fbtn").forEach(b => b.onclick = () => { filter = b.dataset.f; render(); });
}
document.getElementById("search").oninput = e => { search = e.target.value; render(); };
document.getElementById("reset").onclick = () => { if (confirm("Discard all edits and reload the proposals?")) { localStorage.removeItem(STORE_KEY); location.reload(); } };
document.getElementById("download").onclick = () => {
  const invalid = state.filter(rowInvalid);
  if (invalid.length && !confirm(`${invalid.length} kept 'about' row(s) are missing a valid film or event_type and will be SKIPPED. Download anyway?`)) return;
  const out = state.filter(r => r.keep && !rowInvalid(r)).map(r => {
    const o = {
      url: r.url, source: r.source, title: r.title, summary: r.summary || "",
      relation: r.relation,
      expected_film_tmdb_id: r.relation === "about" ? r.tmdb_id : null,
      event_type: r.relation === "about" ? r.event_type : null,
      event_group: r.event_group || null,
    };
    if (r.untracked) o.untracked_film = true;
    return o;
  });
  const blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "validation_set.json"; a.click();
};
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
