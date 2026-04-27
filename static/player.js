/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  const playerId = getPlayerIdFromUrl();
  if (!playerId) {
    showError("Invalid player URL.");
    return;
  }
  // Load watch log first so _watchByYear is ready for renderProfile
  await loadWatchLog(playerId);
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

    td(tr, year);                                                        // Season year
    td(tr, reg?.age ?? play?.age ?? "—");                                // Age
    td(tr, reg  ? fmtMinutes(reg.minutes)     : "—");                   // Reg minutes
    kyleTd(tr,  reg  ? reg.kyle_rating        : null);                  // Reg K.Y.L.E.
    td(tr, play ? (play.playoff_games ?? "—") : "—");                   // Playoff GP
    kyleTd(tr,  play ? play.kyle_rating        : null);                 // Playoff K.Y.L.E.

    // Watch K.Y.L.E. for this year
    const wyl = _watchByYear[year];
    watchKyleTd(tr, wyl ? wyl.watch_kyle : null, wyl ? wyl.best_pct : null, wyl ? wyl.total_watched : 0);

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
    statTd(tr, s.on_off_diff,      "dec", s.on_off_asterisk ? "Fewer than 3% of team games missed; on/off diff replaced with average of other K.Y.L.E. components" : null);
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

function statTd(tr, val, fmt, tooltip) {
  const cell = document.createElement("td");
  if (val === null || val === undefined) {
    cell.textContent = "—";
  } else {
    const n = parseFloat(val);
    let text;
    if (fmt === "pct") {
      text = n.toFixed(1) + "%";
    } else {
      text = n.toFixed(1);
    }
    if (tooltip) {
      text += " *";
      cell.title = tooltip;
      cell.style.cursor = "help";
    }
    cell.textContent = text;
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

function watchKyleTd(tr, kyleVal, pct, totalWatched) {
  const cell = document.createElement("td");
  if (totalWatched === 0 || kyleVal === null || kyleVal === undefined) {
    cell.textContent = "—";
    cell.style.color = "#8b949e";
  } else {
    const score = parseFloat(kyleVal);
    const pctStr = pct != null ? pct.toFixed(1) + "%" : "—";
    cell.textContent = (score >= 0 ? "+" : "") + score.toFixed(3);
    cell.title = `Best player in ${pct != null ? pct.toFixed(1) : "?"}% of ${totalWatched} watched game${totalWatched === 1 ? "" : "s"}`;
    cell.className = score > 0 ? "kyle-pos" : score < 0 ? "kyle-neg" : "";
  }
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

/* ── Watch Log ─────────────────────────────────────────────────── */
let _watchByYear = {};   // populated by loadWatchLog, used by renderSummary

async function loadWatchLog(playerId) {
  try {
    const data = await apiFetch(`/api/player/${playerId}/watch_log`);
    _watchByYear = data.watch_by_year || {};
    renderWatchLog(data);
  } catch (e) {
    // silently fail — watch log is optional
  }
}

function renderWatchLog(data) {
  const section = document.getElementById("section-watchlog");

  const { best_player_count, best_player_games, important_player_games,
          total_watched, best_player_pct, watch_kyle } = data;

  const heroEl = document.getElementById("watchlog-best-count");
  if (best_player_count > 0) {
    heroEl.textContent = `🏆 Best Player in ${best_player_count} watched game${best_player_count === 1 ? "" : "s"}`;
  } else {
    heroEl.textContent = "No games as Best Player yet.";
  }

  const kyleEl = document.getElementById("watchlog-watch-kyle");
  if (total_watched > 0 && watch_kyle != null) {
    const score   = parseFloat(watch_kyle);
    const pctStr  = best_player_pct != null ? best_player_pct.toFixed(1) + "%" : "—";
    const scoreStr = (score >= 0 ? "+" : "") + score.toFixed(3);
    kyleEl.textContent = `Watch K.Y.L.E.: ${scoreStr}  (${pctStr} best across ${total_watched} game${total_watched === 1 ? "" : "s"} watched)`;
    kyleEl.style.color = score > 0 ? "#3fb950" : score < 0 ? "#f85149" : "";
  } else {
    kyleEl.textContent = "Watch K.Y.L.E.: 0.000  (not watched)";
    kyleEl.style.color = "#8b949e";
  }

  if (best_player_games.length) {
    const wrap  = document.getElementById("watchlog-best-wrap");
    const tbody = document.getElementById("watchlog-best-body");
    wrap.classList.remove("hidden");
    tbody.innerHTML = "";
    for (const g of best_player_games) {
      const tr = document.createElement("tr");
      wlTd(tr, g.game_year);
      wlTd(tr, `${g.home_team} vs ${g.away_team}`);
      wlTd(tr, g.round);
      wlTd(tr, `Game ${g.game_of_round}`);
      wlTd(tr, g.date_watched);
      wlTd(tr, g.notes || "");
      tbody.appendChild(tr);
    }
  }

  if (important_player_games.length) {
    const wrap  = document.getElementById("watchlog-imp-wrap");
    const tbody = document.getElementById("watchlog-imp-body");
    wrap.classList.remove("hidden");
    tbody.innerHTML = "";
    for (const g of important_player_games) {
      const tr = document.createElement("tr");
      wlTd(tr, g.game_year);
      wlTd(tr, `${g.home_team} vs ${g.away_team}`);
      wlTd(tr, g.round);
      wlTd(tr, `Game ${g.game_of_round}`);
      if (g.best_player_name) {
        const cell = document.createElement("td");
        const a = document.createElement("a");
        a.href = `/player/${g.best_player_id}`;
        a.className = "player-link";
        a.textContent = g.best_player_name;
        cell.appendChild(a);
        tr.appendChild(cell);
      } else {
        wlTd(tr, "—");
      }
      wlTd(tr, g.date_watched);
      tbody.appendChild(tr);
    }
  }

  if (best_player_count > 0 || best_player_games.length || important_player_games.length || total_watched > 0) {
    section.classList.remove("hidden");
  }
}

function wlTd(tr, text) {
  const td = document.createElement("td");
  td.textContent = text ?? "—";
  tr.appendChild(td);
  return td;
}
