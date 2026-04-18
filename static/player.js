/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  const playerId = getPlayerIdFromUrl();
  if (!playerId) {
    showError("Invalid player URL.");
    return;
  }
  await loadPlayer(playerId);
});

function getPlayerIdFromUrl() {
  const match = window.location.pathname.match(/\/player\/(\d+)/);
  return match ? parseInt(match[1]) : null;
}

/* ── Fetch player history ──────────────────────────────────────── */
async function loadPlayer(playerId) {
  const spinner = document.getElementById("spinner");
  const msg     = document.getElementById("update-msg");
  spinner.classList.remove("hidden");
  msg.textContent = "Loading…";
  msg.className   = "update-msg";

  try {
    const data = await apiFetch(`/api/player/${playerId}`);
    msg.textContent = "";
    renderProfile(data);
  } catch (err) {
    showError(err.message);
  } finally {
    spinner.classList.add("hidden");
  }
}

/* ── Render ────────────────────────────────────────────────────── */
function renderProfile(data) {
  const { player, seasons } = data;

  // Header
  document.title = `K.Y.L.E. — ${player.name}`;
  document.getElementById("player-name").textContent = player.name;
  const metaParts = [];
  if (player.birthdate) metaParts.push(`Born ${player.birthdate}`);
  document.getElementById("player-meta").textContent = metaParts.join(" · ");
  document.getElementById("player-header").classList.remove("hidden");

  if (!seasons.length) {
    document.getElementById("empty-msg").classList.remove("hidden");
    return;
  }

  const regular  = seasons.filter(s => s.season_type === "regular");
  const playoffs = seasons.filter(s => s.season_type === "playoffs");

  // Build a lookup: season_year -> {regular, playoffs}
  const byYear = {};
  for (const s of regular)  (byYear[s.season_year] ??= {}).regular  = s;
  for (const s of playoffs) (byYear[s.season_year] ??= {}).playoffs = s;
  const years = Object.keys(byYear).map(Number).sort((a, b) => b - a);

  renderSummary(years, byYear);
  if (regular.length)  renderStatTable("regular-body",  regular,  false);
  if (playoffs.length) renderStatTable("playoffs-body", playoffs, true);

  if (regular.length)  document.getElementById("section-regular").classList.remove("hidden");
  if (playoffs.length) document.getElementById("section-playoffs").classList.remove("hidden");
}

/* ── Table 1: Summary ──────────────────────────────────────────── */
function renderSummary(years, byYear) {
  const tbody = document.getElementById("summary-body");
  tbody.innerHTML = "";

  for (const year of years) {
    const reg  = byYear[year].regular;
    const play = byYear[year].playoffs;

    const tr = document.createElement("tr");

    // Season label (use whichever row we have)
    const labelSeason = reg || play;
    td(tr, year);                                                        // Season year
    td(tr, reg?.age ?? play?.age ?? "—");                                // Age
    td(tr, reg  ? fmtMinutes(reg.minutes)     : "—");                   // Reg minutes
    kyleTd(tr,  reg  ? reg.kyle_rating        : null);                  // Reg K.Y.L.E.
    td(tr, play ? (play.playoff_games ?? "—") : "—");                   // Playoff GP
    kyleTd(tr,  play ? play.kyle_rating        : null);                 // Playoff K.Y.L.E.

    tbody.appendChild(tr);
  }

  document.getElementById("section-summary").classList.remove("hidden");
}

/* ── Tables 2 & 3: Full stats ──────────────────────────────────── */
function renderStatTable(tbodyId, rows, isPlayoffs) {
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = "";

  // Sorted newest first
  const sorted = [...rows].sort((a, b) => b.season_year - a.season_year);

  for (const s of sorted) {
    const tr = document.createElement("tr");

    td(tr, s.season_year);
    td(tr, s.age ?? "—");
    td(tr, s.position || "—");

    if (isPlayoffs) {
      td(tr, s.playoff_games ?? "—");
    } else {
      td(tr, fmtMinutes(s.minutes));
    }

    statTd(tr, s.usage_rate,       "pct");
    statTd(tr, s.true_shooting_pct ? s.true_shooting_pct * 2 : null, "dec");  // pts/shot = TS%*2
    statTd(tr, s.assist_rate,      "pct");
    statTd(tr, s.turnover_pct,     "pct");
    statTd(tr, s.on_court_rating,  "dec");
    statTd(tr, s.on_off_diff,      "dec");
    statTd(tr, s.bpm,              "dec");
    statTd(tr, s.defense,          "dec");
    kyleTd(tr, s.kyle_rating);

    tbody.appendChild(tr);
  }
}

/* ── Cell helpers ──────────────────────────────────────────────── */
function td(tr, text) {
  const cell = document.createElement("td");
  cell.textContent = (text === null || text === undefined) ? "—" : text;
  tr.appendChild(cell);
  return cell;
}

function statTd(tr, val, fmt) {
  const cell = document.createElement("td");
  if (val === null || val === undefined) {
    cell.textContent = "—";
  } else {
    const n = parseFloat(val);
    if (fmt === "pct") {
      cell.textContent = n.toFixed(1) + "%";
    } else {
      cell.textContent = n.toFixed(1);
    }
  }
  tr.appendChild(cell);
  return cell;
}

function kyleTd(tr, val) {
  const cell = document.createElement("td");
  cell.className = "kyle-cell";
  const div = document.createElement("div");
  div.className = "norm-val";
  div.textContent = (val === null || val === undefined) ? "—" : parseFloat(val).toFixed(2);
  cell.appendChild(div);
  tr.appendChild(cell);
  return cell;
}

function fmtMinutes(val) {
  if (val === null || val === undefined) return "—";
  return Math.round(parseFloat(val)).toLocaleString();
}

/* ── Error state ───────────────────────────────────────────────── */
function showError(msg) {
  const el = document.getElementById("update-msg");
  el.textContent = `✗ ${msg}`;
  el.className = "update-msg error";
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
