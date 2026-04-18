/* ── State ─────────────────────────────────────────────────────── */
let currentSeasonId = null;
let currentSeasonType = "regular";   // "regular" | "playoffs"
let allPlayers = [];          // [{id, name}] for autocomplete
let selectedData = [];        // latest /api/selected response
let sortCol = "kyle_rating";
let sortDir = "desc";         // "asc" | "desc"

/* ── Column definitions ─────────────────────────────────────────── */
const COLUMNS = [
  { key: "name",            label: "Player",         sub: "",               editable: false, isName: true },
  { key: "position",        label: "Position",       sub: "",               editable: "select" },
  { key: "playoff_games",   label: "GP",             sub: "playoff games",  editable: false, playoffsOnly: true },
  { key: "minutes",         label: "Minutes",        sub: "total MP",       editable: false },
  { key: "usage_rate",      label: "Usage%",         sub: "USG%",           editable: false },
  { key: "points_per_shot", label: "Pts/Shot",       sub: "TS% × 2",        editable: false },
  { key: "assist_rate",     label: "Assist%",        sub: "AST%",           editable: false },
  { key: "turnover_pct",    label: "TOV%",           sub: "lower = better", editable: false },
  { key: "on_court_rating", label: "On-Court",       sub: "+/- per 100",    editable: false },
  { key: "on_off_diff",     label: "On/Off Diff",    sub: "",               editable: false },
  { key: "bpm",             label: "BPM",            sub: "",               editable: false },
  { key: "defense",         label: "Defense",        sub: "manual",         editable: "number" },
  { key: "kyle_rating",     label: "K.Y.L.E.",       sub: "rating",         editable: false, isKyle: true },
];

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  await loadSeasons();
  setupSearch();

  document.getElementById("update-btn").addEventListener("click", handleUpdate);
  document.getElementById("add-season-btn").addEventListener("click", openAddSeasonModal);
  document.getElementById("add-season-cancel-btn").addEventListener("click", closeAddSeasonModal);
  document.getElementById("add-season-confirm-btn").addEventListener("click", handleAddSeason);
  document.getElementById("clear-btn").addEventListener("click", () => {
    if (selectedData.length === 0) return; // nothing to clear
    document.getElementById("confirm-overlay").classList.remove("hidden");
  });
  document.getElementById("confirm-cancel-btn").addEventListener("click", () => {
    document.getElementById("confirm-overlay").classList.add("hidden");
  });
  document.getElementById("confirm-clear-btn").addEventListener("click", handleClearAll);
  const deleteSeasonBtn = document.getElementById("delete-season-btn");
  if (deleteSeasonBtn) {
    deleteSeasonBtn.addEventListener("click", handleDeleteSeason);
  }
  document.getElementById("season-select").addEventListener("change", e => {
    currentSeasonId = parseInt(e.target.value);
    currentSeasonType = seasonTypeMap[currentSeasonId] || "regular";
    updateNavLinks();
    buildTableHeaders();
    loadAllPlayers();
    loadSelected();
  });
});

/* ── Seasons ───────────────────────────────────────────────────── */
// Map from season id -> season_type for quick lookup
const seasonTypeMap = {};

function updateNavLinks() {
  const allLink = document.querySelector('a.btn-nav[href^="/all"]');
  if (allLink && currentSeasonId) allLink.href = `/all?season_id=${currentSeasonId}`;
}

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
    await loadSelected();
  }
}

/* ── Players (autocomplete pool) ───────────────────────────────── */
async function loadAllPlayers() {
  if (!currentSeasonId) return;
  allPlayers = await apiFetch(`/api/players?season_id=${currentSeasonId}`);
}

/* ── Selected players ──────────────────────────────────────────── */
async function loadSelected() {
  if (!currentSeasonId) return;
  selectedData = await apiFetch(`/api/selected?season_id=${currentSeasonId}`);
  renderTable();
}

/* ── Update button ─────────────────────────────────────────────── */
async function handleUpdate() {
  const btn = document.getElementById("update-btn");
  const spinner = document.getElementById("spinner");
  const msg = document.getElementById("update-msg");

  btn.disabled = true;
  spinner.classList.remove("hidden");
  msg.textContent = "Fetching data from basketball-reference…";
  msg.className = "update-msg";

  try {
    const res = await apiFetch(`/api/update?season_id=${currentSeasonId}`, { method: "POST" });
    msg.textContent = `✓ Updated — ${res.players_upserted} players`;
    msg.className = "update-msg success";
    await loadAllPlayers();
    await loadSelected();
  } catch (err) {
    msg.textContent = `✗ ${err.message}`;
    msg.className = "update-msg error";
  } finally {
    btn.disabled = false;
    spinner.classList.add("hidden");
  }
}

/* ── Clear All button ──────────────────────────────────────────── */
async function handleClearAll() {
  const overlay = document.getElementById("confirm-overlay");
  const btn = document.getElementById("confirm-clear-btn");
  btn.disabled = true;

  try {
    await apiFetch(`/api/selected?season_id=${currentSeasonId}`, { method: "DELETE" });
    await loadSelected();
  } catch (err) {
    const msg = document.getElementById("update-msg");
    msg.textContent = `✗ ${err.message}`;
    msg.className = "update-msg error";
  } finally {
    overlay.classList.add("hidden");
    btn.disabled = false;
  }
}

/* ── Add Season modal ──────────────────────────────────────────── */
function openAddSeasonModal() {
  const overlay = document.getElementById("add-season-overlay");
  const errEl = document.getElementById("add-season-error");
  // Reset to current year by default
  document.getElementById("new-season-year").value = new Date().getFullYear();
  document.getElementById("new-season-type").value = "regular";
  errEl.textContent = "";
  errEl.classList.add("hidden");
  document.getElementById("add-season-confirm-btn").disabled = false;
  overlay.classList.remove("hidden");
  document.getElementById("new-season-year").focus();
}

function closeAddSeasonModal() {
  document.getElementById("add-season-overlay").classList.add("hidden");
}

async function handleAddSeason() {
  const yearInput = document.getElementById("new-season-year");
  const typeInput = document.getElementById("new-season-type");
  const errEl = document.getElementById("add-season-error");
  const confirmBtn = document.getElementById("add-season-confirm-btn");
  const spinner = document.getElementById("spinner");
  const msg = document.getElementById("update-msg");

  const year = parseInt(yearInput.value);
  const seasonType = typeInput.value;

  if (!year || year < 1990 || year > 2100) {
    errEl.textContent = "Please enter a valid year (1990–2100).";
    errEl.classList.remove("hidden");
    return;
  }

  errEl.classList.add("hidden");
  confirmBtn.disabled = true;
  spinner.classList.remove("hidden");
  msg.textContent = "Creating season…";
  msg.className = "update-msg";

  try {
    // 1. Create the season row
    const season = await apiFetch("/api/seasons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ season_year: year, season_type: seasonType }),
    });

    // 2. Add to the <select> and switch to it
    const sel = document.getElementById("season-select");
    const opt = document.createElement("option");
    opt.value = season.id;
    opt.textContent = season.label;
    // Insert at the top (seasons are ordered DESC by year)
    sel.insertBefore(opt, sel.firstChild);
    currentSeasonId = season.id;
    seasonTypeMap[season.id] = season.season_type;
    currentSeasonType = season.season_type;
    sel.value = currentSeasonId;
    buildTableHeaders();

    closeAddSeasonModal();

    // 3. Auto-scrape the new season
    msg.textContent = `Fetching ${season.label} data from basketball-reference…`;

    const res = await apiFetch(`/api/update?season_id=${currentSeasonId}`, { method: "POST" });
    msg.textContent = `✓ ${season.label} added — ${res.players_upserted} players`;
    msg.className = "update-msg success";

    await loadAllPlayers();

    // 4. Auto-select players from the nearest existing season
    try {
      const nearest = await apiFetch(`/api/seasons/${currentSeasonId}/nearest_selected`);
      if (nearest.player_ids && nearest.player_ids.length > 0) {
        msg.textContent = `Copying ${nearest.player_ids.length} players from nearest season…`;
        await Promise.all(
          nearest.player_ids.map(pid =>
            apiFetch("/api/selected", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ player_id: pid, season_id: currentSeasonId }),
            }).catch(() => {/* ignore players not in this season's stats */})
          )
        );
        msg.textContent = `✓ ${season.label} added — ${res.players_upserted} players, ${nearest.player_ids.length} players auto-selected`;
      }
    } catch (_) {
      // Nearest-season copy is best-effort; don't block on failure
    }

    await loadSelected();
  } catch (err) {
    closeAddSeasonModal();
    msg.textContent = `✗ ${err.message}`;
    msg.className = "update-msg error";
  } finally {
    confirmBtn.disabled = false;
    spinner.classList.add("hidden");
  }
}

/* ── Delete season ─────────────────────────────────────────────── */
async function handleDeleteSeason() {
  if (!currentSeasonId) return;

  const confirmed = window.confirm("Delete this season and all its data? This cannot be undone.");
  if (!confirmed) return;

  const sel = document.getElementById("season-select");

  try {
    await apiFetch(`/api/seasons/${currentSeasonId}`, { method: "DELETE" });

    // Remove the option from the select
    const optToRemove = sel.querySelector(`option[value="${currentSeasonId}"]`);
    if (optToRemove) optToRemove.remove();

    // Pick a new current season if any remain
    if (sel.options.length) {
      currentSeasonId = parseInt(sel.options[0].value);
      sel.value = currentSeasonId;
      await loadAllPlayers();
      await loadSelected();
    } else {
      currentSeasonId = null;
      selectedData = [];
      renderTable();
    }
  } catch (err) {
    const msg = document.getElementById("update-msg");
    if (msg) {
      msg.textContent = `✗ ${err.message}`;
      msg.className = "update-msg error";
    } else {
      alert(err.message || "Failed to delete season");
    }
  }
}

/* ── Table headers ─────────────────────────────────────────────── */
function buildTableHeaders() {
  const headerRow = document.getElementById("header-row");
  const subRow = document.getElementById("subheader-row");
  headerRow.innerHTML = "";
  subRow.innerHTML = "";

  for (const col of COLUMNS) {
    // Hide playoffs-only columns for regular seasons
    if (col.playoffsOnly && currentSeasonType !== "playoffs") continue;

    // main header
    const th = document.createElement("th");
    th.textContent = col.label;
    th.dataset.key = col.key;
    if (!col.isName) {
      th.addEventListener("click", () => handleSort(col.key));
    }
    if (col.key === sortCol) th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    headerRow.appendChild(th);

    // sub-header
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
  // Update header indicators
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

  if (!selectedData.length) {
    emptyMsg.classList.remove("hidden");
    return;
  }
  emptyMsg.classList.add("hidden");

  // Sort
  const sorted = [...selectedData].sort((a, b) => {
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
    // Skip playoffs-only columns for regular seasons
    if (col.playoffsOnly && currentSeasonType !== "playoffs") continue;

    const td = document.createElement("td");

    if (col.isName) {
      td.className = "name-cell";
      td.textContent = player.name;

      // Remove button
      const btn = document.createElement("button");
      btn.className = "btn-remove";
      btn.textContent = "✕";
      btn.title = "Remove from selected";
      btn.addEventListener("click", () => removePlayer(player.selected_id));
      td.appendChild(document.createElement("br"));
      td.appendChild(btn);

    } else if (col.isKyle) {
      td.className = "kyle-cell";
      const norm = document.createElement("div");
      norm.className = "norm-val";
      norm.textContent = fmt(player.kyle_rating);
      td.appendChild(norm);

    } else if (col.key === "position") {
      // Inline-editable position dropdown
      renderPositionCell(td, player);

    } else if (col.playoffsOnly) {
      // Playoff-specific display-only integer (e.g. games played)
      const val = player[col.key];
      td.textContent = (val !== null && val !== undefined) ? val : "—";

    } else if (col.editable === "number") {
      // Inline-editable number (defense)
      renderEditableNumberCell(td, player, col.key);

    } else {
      // Regular stat cell: norm value + raw below
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

/* ── Position cell ─────────────────────────────────────────────── */
function renderPositionCell(td, player) {
  const positions = ["Guard", "Forward", "Center"];
  const current = player.position || "";

  const span = document.createElement("span");
  span.className = "editable";
  span.textContent = current || "—";
  span.title = "Click to edit position";

  span.addEventListener("click", () => {
    const sel = document.createElement("select");
    sel.className = "inline-select";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "—";
    sel.appendChild(blank);
    for (const pos of positions) {
      const opt = document.createElement("option");
      opt.value = pos;
      opt.textContent = pos;
      if (pos === current) opt.selected = true;
      sel.appendChild(opt);
    }
    td.replaceChildren(sel);
    sel.focus();

    const save = async () => {
      const newPos = sel.value || null;
      await patchStats(player.stats_id, { position: newPos });
      player.position = newPos;
      td.replaceChildren();
      renderPositionCell(td, player);
      // Update sort display if we're sorting by position
      if (sortCol === "position") renderTable();
    };

    sel.addEventListener("change", save);
    sel.addEventListener("blur", save);
  });

  td.appendChild(span);
}

/* ── Editable number cell (defense) ────────────────────────────── */
function renderEditableNumberCell(td, player, key) {
  const rawVal = player[key];
  const normVal = player[key + "_norm"];

  const wrap = document.createElement("div");

  const normDiv = document.createElement("div");
  normDiv.className = "norm-val " + (normVal !== null && normVal !== undefined ? colorClass(normVal) : "");
  normDiv.textContent = normVal !== null && normVal !== undefined ? fmt(normVal) : "—";
  wrap.appendChild(normDiv);

  const rawDiv = document.createElement("div");
  rawDiv.className = "raw-val editable";
  rawDiv.textContent = rawVal !== null && rawVal !== undefined ? String(rawVal) : "click to set";
  rawDiv.title = "Click to edit";

  rawDiv.addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "number";
    input.className = "inline-input";
    input.value = rawVal !== null && rawVal !== undefined ? rawVal : "";
    input.step = "0.1";
    wrap.replaceChild(input, rawDiv);
    input.focus();

    const save = async () => {
      const parsed = input.value.trim() === "" ? null : parseFloat(input.value);
      await patchStats(player.stats_id, { [key]: parsed });
      player[key] = parsed;
      // Reload to recalculate norms
      await loadSelected();
    };

    input.addEventListener("keydown", e => {
      if (e.key === "Enter") save();
      if (e.key === "Escape") {
        wrap.replaceChild(rawDiv, input);
      }
    });
    input.addEventListener("blur", save);
  });

  wrap.appendChild(rawDiv);
  td.appendChild(wrap);
}

/* ── Remove player ─────────────────────────────────────────────── */
async function removePlayer(selectedId) {
  await apiFetch(`/api/selected/${selectedId}`, { method: "DELETE" });
  await loadSelected();
}

/* ── Add player (search) ───────────────────────────────────────── */
function setupSearch() {
  const input = document.getElementById("player-search");
  const list = document.getElementById("player-suggestions");
  let activeIndex = -1;

  function showSuggestions(query) {
    list.innerHTML = "";
    activeIndex = -1;
    if (!query) { list.classList.add("hidden"); return; }

    const q = query.toLowerCase();
    const selectedIds = new Set(selectedData.map(p => p.player_id));
    const matches = allPlayers.filter(p =>
      p.name.toLowerCase().includes(q) && !selectedIds.has(p.id)
    ).slice(0, 12);

    if (!matches.length) { list.classList.add("hidden"); return; }

    for (const player of matches) {
      const li = document.createElement("li");
      li.textContent = player.name;
      li.addEventListener("mousedown", e => {
        e.preventDefault(); // prevent blur first
        addPlayer(player);
        input.value = "";
        list.classList.add("hidden");
      });
      list.appendChild(li);
    }
    list.classList.remove("hidden");
  }

  input.addEventListener("input", e => showSuggestions(e.target.value));
  input.addEventListener("blur", () => setTimeout(() => list.classList.add("hidden"), 150));
  input.addEventListener("focus", e => showSuggestions(e.target.value));

  input.addEventListener("keydown", e => {
    const items = list.querySelectorAll("li");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      const playerName = items[activeIndex].textContent;
      const player = allPlayers.find(p => p.name === playerName);
      if (player) { addPlayer(player); input.value = ""; list.classList.add("hidden"); }
      return;
    } else { return; }
    items.forEach((li, i) => li.classList.toggle("active", i === activeIndex));
  });
}

async function addPlayer(player) {
  await apiFetch("/api/selected", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_id: player.id, season_id: currentSeasonId }),
  });
  await loadSelected();
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

async function patchStats(statsId, data) {
  await apiFetch(`/api/stats/${statsId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
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
  if (v >= 0.5)  return "pos-hi";
  if (v >= 0)    return "pos-med";
  if (v >= -0.5) return "neg-med";
  return "neg-hi";
}
