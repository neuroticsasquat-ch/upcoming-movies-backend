"""Generate a self-contained HTML UI for hand-correcting Stage-2 gold `event_group` labels
(NEU-300). Reads the proposer output (the full fixture, with `event_group` proposed on the
linkable `about` rows), groups the `about` rows by film, and lets you merge/split beats by
slug (color-grouped, with a per-film autocomplete of existing beats), plus flip
`is_production_news` / `relation` for the ground-truth refinement.

Run in the container:

    docker compose exec -T upmovies-backend python scripts/build_eventgroup_review.py \\
        < tests/fixtures/link/validation_eventgroups.json \\
        > tests/fixtures/link/eventgroup_review.html

Open `tests/fixtures/link/eventgroup_review.html` in a browser (file://). Edits autosave to
localStorage. "Download validation_set.json" reconstructs the FULL fixture — every row and
every field preserved; only the edited `event_group` / `is_production_news` /
`exclusion_category` / `relation` change. Drop it at tests/fixtures/link/validation_set.json."""

# ruff: noqa: E501  -- this module is mostly an embedded HTML/CSS/JS template

import asyncio
import json
import sys

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.db import SessionLocal

EXCLUSION_CATEGORIES = [
    "reaction",
    "roundup",
    "streaming-move",
    "interview-quote",
    "downstream",
    "other",
]


async def _titles() -> dict[int, str]:
    async with SessionLocal() as s:
        films = (await s.execute(select(Film.tmdb_id, Film.title, Film.release_date))).all()
    out: dict[int, str] = {}
    for tmdb_id, title, rd in films:
        year = rd.year if rd else None
        out[tmdb_id] = f"{title} ({year})" if year else title
    return out


async def main() -> None:
    rows = json.load(sys.stdin)
    titles = await _titles()
    about_idx = [i for i, r in enumerate(rows) if r.get("relation") == "about"]
    payload = {
        "rows": rows,  # full fixture, all fields — preserved verbatim on download
        "aboutIdx": about_idx,  # the rows the UI edits
        "titles": {str(k): v for k, v in titles.items()},  # tmdb_id -> "Title (year)"
        "exclusionCategories": EXCLUSION_CATEGORIES,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE.replace("/*__DATA__*/null", data)
    sys.stdout.buffer.write(html.encode("utf-8"))
    n_films = len({rows[i].get("expected_film_tmdb_id") for i in about_idx})
    print(f"{len(rows)} rows, {len(about_idx)} about rows across {n_films} films", file=sys.stderr)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>event_group review (NEU-300)</title>
<style>
  :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { margin: 0; background: #f4f5f7; color: #1a1a1a; }
  header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd;
    padding: 10px 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; z-index: 10; }
  header strong { font-size: 15px; }
  .stats { font-size: 13px; color: #444; }
  .stats b { color: #000; }
  .grow { flex: 1; }
  button, select, input { font-size: 14px; }
  button { padding: 6px 12px; border: 1px solid #bbb; border-radius: 6px; background: #fff; cursor: pointer; }
  button.primary { background: #1f6feb; color: #fff; border-color: #1f6feb; font-weight: 600; }
  #filmFilter { padding: 6px 10px; border: 1px solid #bbb; border-radius: 6px; min-width: 200px; }
  .saved { font-size: 12px; color: #16a34a; min-width: 50px; }
  main { padding: 16px; max-width: 980px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }
  .film > h2 { margin: 0 0 2px; font-size: 17px; }
  .film .fmeta { font-size: 12px; color: #666; margin-bottom: 8px; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .chip { font-size: 11px; padding: 2px 9px; border-radius: 11px; font-weight: 600; border: 1px solid; }
  .cards { display: flex; flex-direction: column; gap: 8px; }
  .card { background: #fff; border: 1px solid #e0e0e0; border-left-width: 5px; border-radius: 8px; padding: 10px 13px; }
  .card.excluded { opacity: 0.55; }
  .card .top { display: flex; gap: 8px; align-items: center; font-size: 12px; color: #666; margin-bottom: 3px; flex-wrap: wrap; }
  .etbadge { background: #eef2ff; color: #3730a3; padding: 1px 7px; border-radius: 10px; font-weight: 600; font-size: 11px; }
  .exbadge { background: #fee2e2; color: #991b1b; padding: 1px 7px; border-radius: 10px; font-weight: 600; font-size: 11px; }
  .card h3 { margin: 1px 0 4px; font-size: 14px; line-height: 1.35; }
  .card .summary { font-size: 12.5px; color: #444; line-height: 1.45; margin: 0 0 7px; max-height: 3.4em; overflow: hidden; }
  .card .summary.open { max-height: none; }
  .card a.src { color: #1f6feb; text-decoration: none; font-size: 12px; }
  .more { font-size: 11px; color: #888; cursor: pointer; margin-left: 6px; }
  .controls { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; border-top: 1px dashed #eee; padding-top: 7px; margin-top: 6px; }
  .controls label { font-size: 12px; color: #555; display: flex; gap: 4px; align-items: center; }
  .beat input { padding: 5px 8px; border: 1px solid #bbb; border-radius: 6px; min-width: 230px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<header>
  <strong>event_group review</strong>
  <input id="filmFilter" placeholder="filter films by title…" autocomplete="off">
  <span class="grow"></span>
  <span class="stats" id="stats"></span>
  <span class="saved" id="saved"></span>
  <button id="reset">Reset edits</button>
  <button class="primary" id="download">Download validation_set.json</button>
</header>
<main id="list"></main>
<script>
const DATA = /*__DATA__*/null;
const { rows, aboutIdx, titles, exclusionCategories } = DATA;
const STORE_KEY = "upmovies-eventgroup-review-v1";

let edits = {};
try { edits = JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) {}
let filmFilter = "";

function cur(i) {
  const r = rows[i], e = edits[r.url] || {};
  return {
    i, url: r.url, source: r.source, title: r.title, summary: r.summary || "",
    tmdb: r.expected_film_tmdb_id, event_type: r.event_type,
    event_group: e.event_group !== undefined ? e.event_group : (r.event_group || ""),
    relation: e.relation !== undefined ? e.relation : r.relation,
    // null = treated as production news (clusters); false = excluded (not-news)
    is_news: e.is_production_news !== undefined ? e.is_production_news
             : (r.is_production_news === false ? false : null),
    excl: e.exclusion_category !== undefined ? e.exclusion_category : (r.exclusion_category || ""),
  };
}
function setEdit(url, patch) {
  edits[url] = Object.assign({}, edits[url], patch);
  localStorage.setItem(STORE_KEY, JSON.stringify(edits));
  const el = document.getElementById("saved");
  el.textContent = "saved ✓";
  clearTimeout(setEdit._t); setEdit._t = setTimeout(() => el.textContent = "", 1000);
}

function hue(s) { let h = 0; for (const c of (s || "")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function tint(s) { return s ? `hsl(${hue(s)},72%,95%)` : "#fff"; }
function edge(s) { return s ? `hsl(${hue(s)},55%,52%)` : "#cbd5e1"; }
function esc(s) { return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function escA(s) { return (s || "").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }
function comb2(n) { return n < 2 ? 0 : (n * (n - 1)) / 2; }

// about rows grouped by film, ordered by row count desc
const byFilm = {};
aboutIdx.forEach(i => { const t = rows[i].expected_film_tmdb_id; (byFilm[t] ||= []).push(i); });
const filmOrder = Object.keys(byFilm).sort((a, b) => byFilm[b].length - byFilm[a].length);

function newsBeatsFor(idxs) {
  // distinct beat -> count, over the NEWS rows of a film (excluded rows don't cluster)
  const m = {};
  idxs.map(cur).filter(s => s.relation === "about" && s.is_news !== false)
      .forEach(s => { const g = s.event_group || "(unassigned)"; m[g] = (m[g] || 0) + 1; });
  return m;
}

function refreshStats() {
  let films = 0, beats = 0, pairs = 0, unassigned = 0;
  filmOrder.forEach(t => {
    const m = newsBeatsFor(byFilm[t]);
    const keys = Object.keys(m);
    if (keys.length) films++;
    keys.forEach(k => {
      if (k === "(unassigned)") { unassigned += m[k]; return; }
      beats++; pairs += comb2(m[k]);
    });
  });
  document.getElementById("stats").innerHTML =
    `films <b>${films}</b> · beats <b>${beats}</b> · est. gold pairs <b>${pairs}</b>` +
    (unassigned ? ` · <span style="color:#dc2626">${unassigned} unassigned</span>` : ` · <span style="color:#16a34a">all assigned</span>`);
}

function render() {
  const list = document.getElementById("list");
  list.innerHTML = "";
  const q = filmFilter.toLowerCase();
  filmOrder.forEach(t => {
    const title = titles[t] || `tmdb ${t}`;
    if (q && !title.toLowerCase().includes(q)) return;
    const idxs = byFilm[t];
    const states = idxs.map(cur).sort((a, b) =>
      (a.event_group || "~").localeCompare(b.event_group || "~") || a.title.localeCompare(b.title));
    const m = newsBeatsFor(idxs);

    const sec = document.createElement("section");
    sec.className = "film";
    const chips = Object.entries(m).sort((a, b) => b[1] - a[1]).map(([g, c]) => {
      const col = g === "(unassigned)" ? "#dc2626" : edge(g);
      const bg = g === "(unassigned)" ? "#fff" : tint(g);
      return `<span class="chip" style="border-color:${col};background:${bg};color:${col}">${esc(g)} · ${c}${c >= 2 ? ` (${comb2(c)}p)` : ""}</span>`;
    }).join("");
    sec.innerHTML = `<h2>${esc(title)}</h2>
      <div class="fmeta">tmdb ${t} · ${idxs.length} about row(s)</div>
      <div class="chips">${chips}</div>
      <datalist id="dl-${t}">${[...new Set(states.filter(s => s.is_news !== false && s.event_group).map(s => s.event_group))].map(g => `<option value="${escA(g)}">`).join("")}</datalist>
      <div class="cards"></div>`;
    const cards = sec.querySelector(".cards");

    states.forEach(s => {
      const isAbout = s.relation === "about";
      const excluded = isAbout && s.is_news === false;
      const card = document.createElement("div");
      card.className = "card" + (excluded || !isAbout ? " excluded" : "");
      card.style.background = excluded || !isAbout ? "#fff" : tint(s.event_group);
      card.style.borderLeftColor = excluded || !isAbout ? "#cbd5e1" : edge(s.event_group);
      card.innerHTML = `
        <div class="top">
          <span class="etbadge">${esc(s.event_type || "—")}</span>
          ${excluded ? '<span class="exbadge">excluded (not-news)</span>' : ''}
          ${!isAbout ? `<span class="exbadge">${esc(s.relation)}</span>` : ''}
          <span>${esc(s.source)}</span>
        </div>
        <h3>${esc(s.title)}</h3>
        <p class="summary">${esc(s.summary)}</p>
        <div><span class="more">more ▾</span> · <a class="src" href="${escA(s.url)}" target="_blank" rel="noopener">open story ↗</a></div>
        <div class="controls">
          <label class="beat ${isAbout && !excluded ? "" : "hidden"}">beat
            <input list="dl-${t}" value="${escA(s.event_group)}" placeholder="${t}-some-beat">
          </label>
          <label><input type="checkbox" class="ex" ${excluded ? "checked" : ""} ${isAbout ? "" : "disabled"}> not production news</label>
          <label class="excat ${excluded ? "" : "hidden"}">why
            <select>${["", ...exclusionCategories].map(o => `<option value="${o}" ${o === s.excl ? "selected" : ""}>${o || "—"}</option>`).join("")}</select>
          </label>
          <label>relation
            <select class="rel">${["about", "mention", "none"].map(o => `<option ${o === s.relation ? "selected" : ""}>${o}</option>`).join("")}</select>
          </label>
        </div>`;
      const sumEl = card.querySelector(".summary");
      card.querySelector(".more").onclick = e => { sumEl.classList.toggle("open"); e.target.textContent = sumEl.classList.contains("open") ? "less ▴" : "more ▾"; };
      const beatInput = card.querySelector(".beat input");
      if (beatInput) {
        beatInput.oninput = e => setEdit(s.url, { event_group: e.target.value });
        beatInput.onchange = () => render();
      }
      card.querySelector(".ex").onchange = e => {
        setEdit(s.url, { is_production_news: e.target.checked ? false : null,
                         exclusion_category: e.target.checked ? (s.excl || "other") : "" });
        render();
      };
      const excat = card.querySelector(".excat select");
      if (excat) excat.onchange = e => setEdit(s.url, { exclusion_category: e.target.value });
      card.querySelector(".rel").onchange = e => { setEdit(s.url, { relation: e.target.value }); render(); };
      cards.appendChild(card);
    });
    list.appendChild(sec);
  });
  refreshStats();
}

document.getElementById("filmFilter").oninput = e => { filmFilter = e.target.value; render(); };
document.getElementById("reset").onclick = () => {
  if (confirm("Discard all your edits and reload the raw proposals?")) { localStorage.removeItem(STORE_KEY); edits = {}; render(); }
};
document.getElementById("download").onclick = () => {
  const out = rows.map(r => {
    const e = edits[r.url] || {};
    const o = Object.assign({}, r);
    const rel = e.relation !== undefined ? e.relation : r.relation;
    o.relation = rel;
    if (rel === "about") {
      o.event_group = (e.event_group !== undefined ? e.event_group : (r.event_group || "")) || null;
      const news = e.is_production_news !== undefined ? e.is_production_news
                   : (r.is_production_news === false ? false : null);
      if (news === false) {
        o.is_production_news = false;
        o.exclusion_category = (e.exclusion_category !== undefined ? e.exclusion_category : r.exclusion_category) || null;
      } else { delete o.is_production_news; delete o.exclusion_category; }
    } else {
      // demoted out of 'about' — null the about-only fields so the fixture stays valid
      o.expected_film_tmdb_id = null; o.event_type = null; o.event_group = null;
      delete o.is_production_news; delete o.exclusion_category;
    }
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
    asyncio.run(main())
