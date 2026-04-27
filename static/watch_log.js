/* ── Boot ──────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  setDefaultDate();
  await Promise.all([loadLeaderboard(), loadGames()]);
  bindUI();
});

function setDefaultDate() {
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("f-date").value = today;
}

/* ── Helpers ───────────────────────────────────────────────────────── */
async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

function spin(on) {
  document.getElementById("spinner").classList.toggle("hidden", !on);
}

function showMsg(text, isError = false) {
  const el = document.getElementById("update-msg");
  el.textContent = text;
  el.className = "update-msg" + (isError ? " error" : "");
}

/* ── Leaderboard ───────────────────────────────────────────────────── */
let _leaderboard = [];

async function loadLeaderboard() {
  const tbody = document.getElementById("leaderboard-body");
  try {
    _leaderboard = await apiFetch("/api/watched_games/best_player_leaderboard");
    renderLeaderboard();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" style="color:#f85149;">Error: ${esc(e.message)}</td></tr>`;
  }
}

function renderLeaderboard() {
  const tbody = document.getElementById("leaderboard-body");
  const minGames = parseInt(document.getElementById("lb-min-games").value) || 0;
  const rows = _leaderboard.filter(r => r.total_watched_games >= minGames);
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#8b949e;">No data yet</td></tr>';
    return;
  }
  rows.forEach((r, i) => {
    const pctStr   = r.best_player_pct != null ? r.best_player_pct.toFixed(1) : "—";
    const kyle     = r.watch_kyle;
    const kyleStr  = kyle != null ? (kyle >= 0 ? "+" : "") + kyle.toFixed(3) : "0.000";
    const kyleClass = kyle > 0 ? "kyle-pos" : kyle < 0 ? "kyle-neg" : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><a href="/player/${r.player_id}" class="player-link">${esc(r.name)}</a></td>
      <td>${r.r1}</td>
      <td>${r.r2}</td>
      <td>${r.r3}</td>
      <td>${r.r4}</td>
      <td>${r.best_player_count}</td>
      <td>${r.total_watched_games}</td>
      <td>${pctStr}</td>
      <td class="${kyleClass}">${kyleStr}</td>
    `;
    tbody.appendChild(tr);
  });
}

/* ── Game Log ──────────────────────────────────────────────────────── */
let _games = [];

async function loadGames(params = {}) {
  spin(true);
  try {
    const qs = new URLSearchParams();
    if (params.year)       qs.set("year", params.year);
    if (params.round)      qs.set("round", params.round);
    if (params.conference) qs.set("conference", params.conference);
    _games = await apiFetch("/api/watched_games?" + qs.toString());
    renderGames(_games);
    showMsg("");
  } catch (e) {
    showMsg(e.message, true);
  } finally {
    spin(false);
  }
}

function renderGames(games) {
  const tbody = document.getElementById("games-body");
  const empty = document.getElementById("games-empty");
  tbody.innerHTML = "";
  if (!games.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  for (const g of games) {
    const tr = document.createElement("tr");
    tr.dataset.id = g.id;

    const playerNames = (g.important_players || []).map(p =>
      `<a href="/player/${p.player_id}" class="player-link">${esc(p.name)}</a>`
    ).join(", ");

    tr.innerHTML = `
      <td>${esc(g.date_watched)}</td>
      <td>${g.game_year}</td>
      <td>${esc(g.home_team)}</td>
      <td>${esc(g.away_team)}</td>
      <td class="winner-cell">${esc(g.winner_team || "—")}</td>
      <td>${esc(g.conference)}</td>
      <td>${esc(g.round)}</td>
      <td>${g.game_of_round}</td>
      <td>${g.best_player_name
        ? `<a href="/player/${g.best_player_id}" class="player-link">${esc(g.best_player_name)}</a>`
        : "—"}</td>
      <td class="players-cell">${playerNames || "—"}</td>
      <td class="notes-cell">${esc(g.notes || "")}</td>
      <td class="actions-cell">
        <button class="btn btn-sm btn-edit" data-id="${g.id}">Edit</button>
        <button class="btn btn-sm btn-danger btn-delete" data-id="${g.id}">Del</button>
      </td>
    `;
    tbody.appendChild(tr);
  }

  // Edit buttons
  tbody.querySelectorAll(".btn-edit").forEach(btn => {
    btn.addEventListener("click", () => {
      const game = _games.find(g => g.id == btn.dataset.id);
      if (game) openModal(game);
    });
  });

  // Delete buttons
  tbody.querySelectorAll(".btn-delete").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this game log entry?")) return;
      try {
        await apiFetch(`/api/watched_games/${btn.dataset.id}`, { method: "DELETE" });
        await Promise.all([loadLeaderboard(), loadGames(getCurrentFilters())]);
        showMsg("Deleted.");
      } catch (e) {
        showMsg(e.message, true);
      }
    });
  });
}

/* ── Filters ───────────────────────────────────────────────────────── */
function getCurrentFilters() {
  return {
    year:       document.getElementById("filter-year").value || undefined,
    round:      document.getElementById("filter-round").value || undefined,
    conference: document.getElementById("filter-conf").value || undefined,
  };
}

function bindUI() {
  document.getElementById("btn-lb-filter").addEventListener("click", renderLeaderboard);
  document.getElementById("btn-lb-clear").addEventListener("click", () => {
    document.getElementById("lb-min-games").value = "";
    renderLeaderboard();
  });

  document.getElementById("btn-filter").addEventListener("click", () => {
    loadGames({
      year:       document.getElementById("filter-year").value || undefined,
      round:      document.getElementById("filter-round").value || undefined,
      conference: document.getElementById("filter-conf").value || undefined,
    });
  });

  document.getElementById("btn-clear-filter").addEventListener("click", () => {
    document.getElementById("filter-year").value  = "";
    document.getElementById("filter-round").value = "";
    document.getElementById("filter-conf").value  = "";
    loadGames();
  });

  document.getElementById("btn-add-game").addEventListener("click", () => openModal(null));
  document.getElementById("btn-cancel-modal").addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", e => {
    if (e.target === document.getElementById("modal-overlay")) closeModal();
  });

  document.getElementById("game-form").addEventListener("submit", handleFormSubmit);

  // When year changes, reload player list
  document.getElementById("f-year").addEventListener("change", () => loadPlayersForYear(null));
}

/* ── Important-players state ───────────────────────────────────────── */
let _availablePlayers = [];   // all players for the current year
let _selectedPlayers  = [];   // { id, name } objects currently chipped

function renderChips() {
  const container = document.getElementById("player-chips");
  container.innerHTML = "";
  for (const p of _selectedPlayers) {
    const chip = document.createElement("span");
    chip.className = "player-chip";
    chip.innerHTML = `${esc(p.name)} <button type="button" aria-label="Remove">&times;</button>`;
    chip.querySelector("button").addEventListener("click", () => {
      _selectedPlayers = _selectedPlayers.filter(x => x.id !== p.id);
      renderChips();
    });
    container.appendChild(chip);
  }
  renderBestPlayerDropdown();
}

function renderBestPlayerDropdown() {
  const bestSel = document.getElementById("f-best");
  const currentVal = bestSel.value || bestSel.dataset.pendingId || "";
  bestSel.innerHTML = '<option value="">— none —</option>';
  for (const p of _selectedPlayers) {
    const opt = new Option(p.name, p.id);
    if (String(p.id) === String(currentVal)) opt.selected = true;
    bestSel.appendChild(opt);
  }
}

function setupPlayerSearch() {
  const input = document.getElementById("f-player-search");
  const list  = document.getElementById("f-player-suggestions");
  let activeIndex = -1;

  function showSuggestions(query) {
    list.innerHTML = "";
    activeIndex = -1;
    if (!query.trim()) { list.classList.add("hidden"); return; }

    const q = query.toLowerCase();
    const selectedIds = new Set(_selectedPlayers.map(p => p.id));
    const matches = _availablePlayers.filter(p =>
      p.name.toLowerCase().includes(q) && !selectedIds.has(p.id)
    ).slice(0, 12);

    if (!matches.length) { list.classList.add("hidden"); return; }

    for (const player of matches) {
      const li = document.createElement("li");
      li.textContent = player.name;
      li.addEventListener("mousedown", e => {
        e.preventDefault();
        addImportantPlayer(player);
        input.value = "";
        list.classList.add("hidden");
      });
      list.appendChild(li);
    }
    list.classList.remove("hidden");
  }

  input.addEventListener("input",  e => showSuggestions(e.target.value));
  input.addEventListener("focus",  e => showSuggestions(e.target.value));
  input.addEventListener("blur",   () => setTimeout(() => list.classList.add("hidden"), 150));
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
      const player = _availablePlayers.find(p => p.name === playerName);
      if (player) { addImportantPlayer(player); input.value = ""; list.classList.add("hidden"); }
      return;
    } else { return; }
    items.forEach((li, i) => li.classList.toggle("active", i === activeIndex));
  });
}

function addImportantPlayer(player) {
  if (!_selectedPlayers.find(p => p.id === player.id)) {
    _selectedPlayers.push(player);
    renderChips();
  }
}

/* ── Modal ─────────────────────────────────────────────────────────── */
async function openModal(game) {
  document.getElementById("modal-title").textContent = game ? "Edit Game" : "Log a Game";
  document.getElementById("edit-id").value = game ? game.id : "";
  document.getElementById("f-home").value  = game ? game.home_team : "";
  document.getElementById("f-away").value  = game ? game.away_team : "";
  document.getElementById("f-date").value  = game ? game.date_watched : new Date().toISOString().slice(0, 10);
  document.getElementById("f-year").value  = game ? game.game_year : "";
  document.getElementById("f-conf").value  = game ? game.conference : "";
  document.getElementById("f-round").value = game ? game.round : "";
  document.getElementById("f-game").value  = game ? game.game_of_round : "";
  document.getElementById("f-notes").value = game ? (game.notes || "") : "";

  updateWinnerDropdown(game?.home_team || "", game?.away_team || "", game?.winner_team || "");

  // Wire home/away changes to winner dropdown
  document.getElementById("f-home").oninput = () => {
    updateWinnerDropdown(
      document.getElementById("f-home").value,
      document.getElementById("f-away").value,
      document.getElementById("f-winner").value
    );
  };
  document.getElementById("f-away").oninput = () => {
    updateWinnerDropdown(
      document.getElementById("f-home").value,
      document.getElementById("f-away").value,
      document.getElementById("f-winner").value
    );
  };

  // Load players
  await loadPlayersForYear(game);

  document.getElementById("modal-overlay").classList.remove("hidden");
}

function updateWinnerDropdown(home, away, selected) {
  const sel = document.getElementById("f-winner");
  sel.innerHTML = `<option value="">— unknown —</option>`;
  if (home) {
    const o = new Option(home, home);
    if (selected === home) o.selected = true;
    sel.appendChild(o);
  }
  if (away) {
    const o = new Option(away, away);
    if (selected === away) o.selected = true;
    sel.appendChild(o);
  }
}

async function loadPlayersForYear(game) {
  const year     = document.getElementById("f-year").value;
  const loading  = document.getElementById("player-loading");

  // Reset state
  _availablePlayers = [];
  _selectedPlayers  = game?.important_players?.map(p => ({ id: p.player_id, name: p.name })) || [];

  // Pre-set the best player id so renderBestPlayerDropdown() can select it
  const bestSel = document.getElementById("f-best");
  bestSel.dataset.pendingId = game?.best_player_id || "";

  renderChips(); // also calls renderBestPlayerDropdown()

  setupPlayerSearch();

  // Restore best-player selection now that options are built
  if (bestSel.dataset.pendingId) {
    bestSel.value = bestSel.dataset.pendingId;
  }

  if (!year) return;

  loading.style.display = "block";
  try {
    const players = await apiFetch(`/api/players_for_year?year=${year}`);
    loading.style.display = "none";
    _availablePlayers = players.map(p => ({ id: p.id, name: p.name }));
  } catch (e) {
    loading.style.display = "none";
    loading.textContent = "Failed to load players.";
  }
}

function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
}

async function handleFormSubmit(e) {
  e.preventDefault();
  const editId = document.getElementById("edit-id").value;

  const playerIds = _selectedPlayers.map(p => p.id);

  const payload = {
    home_team:      document.getElementById("f-home").value.trim(),
    away_team:      document.getElementById("f-away").value.trim(),
    winner_team:    document.getElementById("f-winner").value || null,
    date_watched:   document.getElementById("f-date").value,
    game_year:      parseInt(document.getElementById("f-year").value),
    conference:     document.getElementById("f-conf").value,
    round:          document.getElementById("f-round").value,
    game_of_round:  parseInt(document.getElementById("f-game").value),
    notes:          document.getElementById("f-notes").value.trim(),
    best_player_id: parseInt(document.getElementById("f-best").value) || null,
    player_ids:     playerIds,
  };

  try {
    if (editId) {
      await apiFetch(`/api/watched_games/${editId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await apiFetch("/api/watched_games", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    closeModal();
    await Promise.all([loadLeaderboard(), loadGames(getCurrentFilters())]);
    showMsg(editId ? "Game updated." : "Game logged.");
  } catch (e) {
    showMsg(e.message, true);
  }
}

/* ── Escape helper ─────────────────────────────────────────────────── */
function esc(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
