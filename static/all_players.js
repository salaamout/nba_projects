/* ── State ─────────────────────────────────────────────────────── */
let currentSeasonId = null;
let currentSeasonType = "regular";   // "regular" | "playoffs"
let allData = [];        // latest /api/all_players response
let sortCol = "kyle_rating";
let sortDir = "desc";

/* ── Column definitions (mirrors main page, minus remove/editable) ── */
const COLUMNS = [
  { key: "name",            label: "Player",         sub: "",               isName: true  },
  { key: "position",        label: "Position",       sub: ""                              },
  { key: "playoff_games",   label: "GP",             sub: "playoff games",  playoffsOnly: true },
  { key: "minutes",         label: "Minutes",        sub: "total MP"                      },
  { key: "usage_rate",      label: "Usage%",         sub: "USG%"                          },
  { key: "points_per_shot", label: "Pts/Shot",       sub: "TS% × 2"                       },
  { key: "assist_rate",     label: "Assist%",        sub: "AST%"                          },
  { key: "turnover_pct",    label: "TOV%",           sub: "lower = better"                },
  { key: "on_court_rating", label: "On-Court",       sub: "+/- per 100"                   },
  { key: "on_off_diff",     label: "On/Off Diff",    sub: ""                              },
  { key: "bpm",             label: "BPM",            sub: ""                              },
  { key: "defense",         label: "Defense",        sub: "manual"                        },
  { key: "kyle_rating",     label: "K.Y.L.E.",       sub: "rating",         isKyle: true  },
  { key: "_add",            label: "",               sub: "",               isAdd: true   },
];

/* ── Seasons map ───────────────────────────────────────────────── */
const seasonTypeMap = {};

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  await loadSeasons();
});
/* ── Seasons ───────────────────────────────────────────────────── */
async function loadSeasons() {
  const seasons = await apiFetch("/api/seasons");
  const sel = document.getElementById("season-select");
  sel.innerHTML = "";
  for (const s of seasons) {
    seasonTypeMap[s.id] = s.season_type;
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.label;
    sel.appendChild(opt);
  }
  if (seasons.length) {
    const params = new URLSearchParams(window.location.search);
    const paramId = parseInt(params.get("season_id"));
    const match = paramId && seasons.find(s => s.id === paramId);
    currentSeasonId = match ? paramId : seasons[0].id;
    currentSeasonType = seasonTypeMap[currentSeasonId] || "regular";
    sel.value = currentSeasonId;
    updateNavLinks();
    buildTableHeaders();
    await loadAllPlayers();
  }

  sel.addEventListener("change", async e => {
    currentSeasonId = parseInt(e.target.value);
    currentSeasonType = seasonTypeMap[currentSeasonId] || "regular";
    updateNavLinks();
    buildTableHeaders();
    await loadAllPlayers();
  });

  document.getElementById("min-minutes").addEventListener("input", () => {
    renderTable();
  });
}

function updateNavLinks() {
  const backLink = document.querySelector('a.btn-nav[href^="/"]');
  if (backLink && currentSeasonId) backLink.href = `/?season_id=${currentSeasonId}`;
}

/* ── Fetch and render all players ──────────────────────────────── */
async function loadAllPlayers() {
  if (!currentSeasonId) return;

  const spinner = document.getElementById("spinner");
  const msg = document.getElementById("update-msg");
  spinner.classList.remove("hidden");
  msg.textContent = "Loading…";
  msg.className = "update-msg";

  try {
    allData = await apiFetch(`/api/all_players?season_id=${currentSeasonId}`);
    msg.textContent = `${allData.length} players`;
    msg.className = "update-msg success";
    renderTable();
  } catch (err) {
    msg.textContent = `✗ ${err.message}`;
    msg.className = "update-msg error";
  } finally {
    spinner.classList.add("hidden");
  }
}

/* ── Table headers ─────────────────────────────────────────────── */
function buildTableHeaders() {
  const headerRow = document.getElementById("header-row");
  const subRow = document.getElementById("subheader-row");
  headerRow.innerHTML = "";
  subRow.innerHTML = "";

  for (const col of COLUMNS) {
    if (col.playoffsOnly && currentSeasonType !== "playoffs") continue;

    const th = document.createElement("th");
    th.textContent = col.label;
    th.dataset.key = col.key;
    if (!col.isName) {
      th.addEventListener("click", () => handleSort(col.key));
    }
    if (col.key === sortCol) th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    headerRow.appendChild(th);

    const th2 = document.createElement("th");
    th2.textContent = col.sub || "";
    subRow.appendChild(th2);
  }
}

function handleSort(key) {
  if (sortCol === key) {
    sortDir = sortDir === "asc" ? "desc" : "asc";
  } else {
    sortCol = key;
    sortDir = "desc";
  }
  document.querySelectorAll("#header-row th").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.key === sortCol) {
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
  renderTable();
}

/* ── Table rendering ───────────────────────────────────────────── */
function renderTable() {
  const tbody = document.getElementById("table-body");
  const emptyMsg = document.getElementById("empty-msg");
  tbody.innerHTML = "";

  const minMin = parseFloat(document.getElementById("min-minutes").value) || 0;
  const filtered = allData.filter(p => (p.minutes ?? 0) >= minMin);

  if (!filtered.length) {
    emptyMsg.classList.remove("hidden");
    return;
  }
  emptyMsg.classList.add("hidden");

  const sorted = [...filtered].sort((a, b) => {
    let va = a[sortCol] ?? null;
    let vb = b[sortCol] ?? null;
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (typeof va === "string") va = va.toLowerCase();
    if (typeof vb === "string") vb = vb.toLowerCase();
    if (va < vb) return sortDir === "asc" ? -1 : 1;
    if (va > vb) return sortDir === "asc" ? 1 : -1;
    return 0;
  });

  for (const player of sorted) {
    tbody.appendChild(buildRow(player));
  }
}

function buildRow(player) {
  const tr = document.createElement("tr");

  for (const col of COLUMNS) {
    if (col.playoffsOnly && currentSeasonType !== "playoffs") continue;

    const td = document.createElement("td");

    if (col.isName) {
      td.className = "name-cell";
      td.textContent = player.name;

    } else if (col.isKyle) {
      td.className = "kyle-cell";
      const norm = document.createElement("div");
      norm.className = "norm-val";
      norm.textContent = fmt(player.kyle_rating);
      td.appendChild(norm);

    } else if (col.isAdd) {
      td.className = "add-cell";
      const btn = document.createElement("button");
      btn.className = "btn-add-player";
      btn.textContent = "+ Add";
      btn.title = `Add ${player.name} to selected players`;
      btn.addEventListener("click", () => addToSelected(player, btn));
      td.appendChild(btn);

    } else if (col.playoffsOnly) {
      // Playoff-specific display-only integer (e.g. games played)
      const val = player[col.key];
      td.textContent = (val !== null && val !== undefined) ? val : "—";

    } else if (col.key === "position") {
      td.textContent = player.position || "—";

    } else {
      const rawVal = player[col.key];
      const normVal = player[col.key + "_norm"];

      if (normVal !== null && normVal !== undefined) {
        const norm = document.createElement("div");
        norm.className = "norm-val " + colorClass(normVal);
        norm.textContent = fmt(normVal);
        td.appendChild(norm);
      }

      if (rawVal !== null && rawVal !== undefined) {
        const raw = document.createElement("div");
        raw.className = "raw-val";
        raw.textContent = fmtRaw(col.key, rawVal);
        td.appendChild(raw);
      }

      if (rawVal === null || rawVal === undefined) {
        td.textContent = "—";
      }
    }

    tr.appendChild(td);
  }
  return tr;
}

/* ── Add player to selected ─────────────────────────────────────── */
async function addToSelected(player, btn) {
  btn.disabled = true;
  btn.textContent = "…";
  try {
    await apiFetch("/api/selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ player_id: player.player_id, season_id: currentSeasonId }),
    });
    btn.textContent = "✓ Added";
    btn.classList.add("btn-add-player--added");
  } catch (err) {
    btn.textContent = "✗";
    btn.title = err.message;
    btn.disabled = false;
  }
}

/* ── API helper ────────────────────────────────────────────────── */
async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

/* ── Formatting helpers ─────────────────────────────────────────── */
function fmt(val) {
  if (val === null || val === undefined) return "—";
  return parseFloat(val).toFixed(2);
}

function fmtRaw(key, val) {
  if (val === null || val === undefined) return "—";
  const n = parseFloat(val);
  if (key === "minutes") return Math.round(n).toLocaleString();
  if (key === "true_shooting_pct") return (n * 100).toFixed(1) + "%";
  if (key === "usage_rate" || key === "assist_rate" || key === "turnover_pct")
    return n.toFixed(1) + "%";
  return n.toFixed(1);
}

function colorClass(norm) {
  const v = parseFloat(norm);
  if (v > 1.0)   return "pos-beyond";
  if (v >= 0.5)  return "pos-hi";
  if (v >= 0)    return "pos-med";
  if (v >= -0.5) return "neg-med";
  if (v >= -1.0) return "neg-hi";
  return "neg-beyond";
}
