/* ── State ─────────────────────────────────────────────────────── */
let tableData  = [];
let sortCol    = "best_window_total";
let sortDir    = "desc";
let windowSize = 3;

/* ── Column definitions (rebuilt when window changes) ──────────── */
let COLUMNS = [];
function updateColumnDefs() {
  COLUMNS = [
    { key: "rank",              label: "#",                         isRank:  true },
    { key: "name",              label: "Player",                    isName:  true },
    { key: "years",             label: "Years",                     isYears: true },
    { key: "regular_total",     label: "Regular K.Y.L.E.",         isKyle:  true },
    { key: "playoffs_total",    label: "Playoffs K.Y.L.E.",        isKyle:  true },
    { key: "best_window_total", label: `Best ${windowSize}-Yr Total`, isTotal: true },
    { key: "watch_kyle_total",  label: "Watch K.Y.L.E.",           isWatch: true },
    { key: "playoff_games",     label: "Playoff Games",            isPlayoffGames: true },
  ];
}

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("window-select").value = windowSize;
  document.getElementById("window-select").addEventListener("change", async (e) => {
    windowSize = parseInt(e.target.value, 10);
    updateColumnDefs();
    buildTableHeaders();
    await loadData();
  });

  document.getElementById("suggest-btn").addEventListener("click", runSuggest);
  document.getElementById("suggest-dismiss").addEventListener("click", () => {
    document.getElementById("suggest-card").classList.add("hidden");
  });

  updateColumnDefs();
  buildTableHeaders();
  await loadData();
});

/* ── Fetch ─────────────────────────────────────────────────────── */
async function loadData() {
  const spinner = document.getElementById("spinner");
  const msg     = document.getElementById("update-msg");
  spinner.classList.remove("hidden");
  msg.textContent = "Loading\u2026";
  msg.className   = "update-msg";

  try {
    tableData = await apiFetch(`/api/best3year?window=${windowSize}`);
    msg.textContent = `${tableData.length} player${tableData.length === 1 ? "" : "s"}`;
    msg.className   = "update-msg success";
    renderTable();
  } catch (err) {
    msg.textContent = `\u2717 ${err.message}`;
    msg.className   = "update-msg error";
  } finally {
    spinner.classList.add("hidden");
  }
}

/* ── Suggest Game ──────────────────────────────────────────────── */
async function runSuggest() {
  const spinner = document.getElementById("spinner");
  const card    = document.getElementById("suggest-card");
  const content = document.getElementById("suggest-content");

  spinner.classList.remove("hidden");
  card.classList.add("hidden");

  try {
    const data = await apiFetch(`/api/suggest_game?window=${windowSize}`);
    content.innerHTML = renderSuggestContent(data);
    card.classList.remove("hidden");
  } catch (err) {
    content.innerHTML = `<span class="suggest-error">\u2717 ${err.message}</span>`;
    card.classList.remove("hidden");
  } finally {
    spinner.classList.add("hidden");
  }
}

function renderSuggestContent(data) {
  if (data.result === "found") {
    const g  = data.game;
    const p1 = data.player1;
    const p2 = data.player2;
    const gameInfo = g.team1 && g.team2 ? `${g.team1} vs ${g.team2}` : "Unknown matchup";
    return `
      <div class="suggest-found">
        <div class="suggest-pair">
          <a href="/player/${p1.id}" class="player-link">${p1.name}</a>
          <span class="suggest-peak">${p1.peak} &mdash; ${fmt(p1.score)}</span>
          <span class="suggest-vs">&amp;</span>
          <a href="/player/${p2.id}" class="player-link">${p2.name}</a>
          <span class="suggest-peak">${p2.peak} &mdash; ${fmt(p2.score)}</span>
        </div>
        <div class="suggest-game-info">
          <span class="suggest-game-label">Suggested game:</span>
          <strong>${gameInfo}</strong>
          &bull; ${g.year}${g.game_date ? ` &bull; ${g.game_date}` : ""}
        </div>
        <div class="suggest-pair-score">Pair score: ${fmt(data.pair_score)}</div>
        <a href="/watch_log" class="btn-nav suggest-watchlog">&rarr; Watch Log</a>
      </div>`;
  }
  if (data.result === "missing_data") {
    return `<span class="suggest-error">Appearance data missing for <strong>${data.player}</strong>. Run a scrape on their player page first.</span>`;
  }
  if (data.result === "none") {
    return `<span class="suggest-none">${data.message || "No suggestion available."}</span>`;
  }
  return `<span class="suggest-error">${data.message || "An error occurred."}</span>`;
}

/* ── Table headers ─────────────────────────────────────────────── */
function buildTableHeaders() {
  const headerRow = document.getElementById("header-row");
  headerRow.innerHTML = "";

  for (const col of COLUMNS) {
    const th = document.createElement("th");
    th.textContent = col.label;
    th.dataset.key = col.key;
    if (!col.isRank && !col.isName && !col.isYears) {
      th.addEventListener("click", () => handleSort(col.key));
    }
    if (col.key === sortCol) {
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
    headerRow.appendChild(th);
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
  const tbody    = document.getElementById("table-body");
  const emptyMsg = document.getElementById("empty-msg");
  tbody.innerHTML = "";

  if (!tableData.length) {
    emptyMsg.classList.remove("hidden");
    return;
  }
  emptyMsg.classList.add("hidden");

  const sorted = [...tableData].sort((a, b) => {
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

  sorted.forEach((player, idx) => {
    tbody.appendChild(buildRow(player, idx + 1));
  });
}

function buildRow(player, rank) {
  const tr = document.createElement("tr");

  for (const col of COLUMNS) {
    const td = document.createElement("td");

    if (col.isRank) {
      td.className   = "rank-cell";
      td.textContent = rank;

    } else if (col.isName) {
      td.className = "name-cell";
      const a = document.createElement("a");
      a.className   = "player-link";
      a.href        = `/player/${player.player_id}`;
      a.textContent = player.name;
      td.appendChild(a);

    } else if (col.isYears) {
      td.className   = "year-cell";
      td.textContent = `${player.best_start_year}\u2013${player.best_end_year}`;

    } else if (col.isTotal) {
      td.className = "kyle-cell cumulative-total";
      const div = document.createElement("div");
      div.className   = "norm-val";
      div.textContent = fmt(player.best_window_total);
      td.appendChild(div);

    } else if (col.isWatch) {
      td.className = "kyle-cell";
      const div = document.createElement("div");
      div.className   = "norm-val";
      div.textContent = fmt(player.watch_kyle_total);
      td.appendChild(div);

    } else if (col.isPlayoffGames) {
      td.className = "kyle-cell";
      const watched = player.playoff_watched ?? 0;
      const played  = player.playoff_played  ?? 0;
      td.textContent = `${watched}/${played}`;

    } else {
      td.className = "kyle-cell";
      const div = document.createElement("div");
      div.className   = "norm-val";
      div.textContent = fmt(player[col.key]);
      td.appendChild(div);
    }

    tr.appendChild(td);
  }
  return tr;
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

/* ── Formatting ─────────────────────────────────────────────────── */
function fmt(val) {
  if (val === null || val === undefined) return "\u2014";
  return parseFloat(val).toFixed(2);
}
