/* ── filter.js ────────────────────────────────────────────────────
   Player Filter page
   ───────────────────────────────────────────────────────────────── */

/* ── Field definitions ─────────────────────────────────────────── */
const RAW_FIELDS = [
  { value: "minutes",         label: "Minutes"      },
  { value: "usage_rate",      label: "Usage %"      },
  { value: "points_per_shot", label: "Pts/Shot"     },
  { value: "assist_rate",     label: "Assist %"     },
  { value: "turnover_pct",    label: "TOV %"        },
  { value: "on_court_rating", label: "On-Court"     },
  { value: "on_off_diff",     label: "On/Off Diff"  },
  { value: "bpm",             label: "BPM"          },
  { value: "defense",         label: "Defense"      },
  { value: "kyle_rating",     label: "K.Y.L.E."     },
];

const NORM_FIELDS = [
  { value: "minutes_norm",         label: "Minutes (pts)"      },
  { value: "usage_rate_norm",      label: "Usage % (pts)"      },
  { value: "points_per_shot_norm", label: "Pts/Shot (pts)"     },
  { value: "assist_rate_norm",     label: "Assist % (pts)"     },
  { value: "turnover_pct_norm",    label: "TOV % (pts)"        },
  { value: "on_court_rating_norm", label: "On-Court (pts)"     },
  { value: "on_off_diff_norm",     label: "On/Off Diff (pts)"  },
  { value: "bpm_norm",             label: "BPM (pts)"          },
  { value: "defense_norm",         label: "Defense (pts)"      },
];

const OPERATORS = [">", ">=", "<", "<=", "="];

/* ── Result table columns ──────────────────────────────────────── */
const COLUMNS = [
  { key: "name",            label: "Player",      sub: "",              isName: true  },
  { key: "season_label",    label: "Season",      sub: "",              isSeason: true },
  { key: "minutes",         label: "Minutes",     sub: "total MP"                     },
  { key: "usage_rate",      label: "Usage%",      sub: "USG%"                         },
  { key: "points_per_shot", label: "Pts/Shot",    sub: "TS% × 2"                      },
  { key: "assist_rate",     label: "Assist%",     sub: "AST%"                         },
  { key: "turnover_pct",    label: "TOV%",        sub: "lower = better"               },
  { key: "on_court_rating", label: "On-Court",    sub: "+/- per 100"                  },
  { key: "on_off_diff",     label: "On/Off Diff", sub: ""                             },
  { key: "bpm",             label: "BPM",         sub: ""                             },
  { key: "defense",         label: "Defense",     sub: "manual"                       },
  { key: "kyle_rating",     label: "K.Y.L.E.",    sub: "rating",        isKyle: true  },
];

/* ── State ─────────────────────────────────────────────────────── */
let allResults = [];
let sortCol    = "kyle_rating";
let sortDir    = "desc";
let filterSerial = 0; // unique id for each filter row

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  buildTableHeaders();
  addFilterRow();   // start with one empty filter row

  document.getElementById("btn-add-filter").addEventListener("click", addFilterRow);
  document.getElementById("btn-search").addEventListener("click", runSearch);
  document.getElementById("btn-clear-filters").addEventListener("click", clearAll);
});

/* ── Filter row management ─────────────────────────────────────── */
function addFilterRow() {
  const id  = ++filterSerial;
  const row = document.createElement("div");
  row.className  = "filter-row";
  row.dataset.id = id;

  // Field select
  const selField = document.createElement("select");
  selField.className = "sel-field";

  const grpRaw = document.createElement("optgroup");
  grpRaw.label = "Raw Stats";
  for (const f of RAW_FIELDS) {
    const opt = document.createElement("option");
    opt.value       = f.value;
    opt.textContent = f.label;
    grpRaw.appendChild(opt);
  }

  const grpNorm = document.createElement("optgroup");
  grpNorm.label = "Kyle Points";
  for (const f of NORM_FIELDS) {
    const opt = document.createElement("option");
    opt.value       = f.value;
    opt.textContent = f.label;
    grpNorm.appendChild(opt);
  }

  selField.appendChild(grpRaw);
  selField.appendChild(grpNorm);

  // Operator select
  const selOp = document.createElement("select");
  selOp.className = "sel-operator";
  for (const op of OPERATORS) {
    const opt = document.createElement("option");
    opt.value       = op;
    opt.textContent = op;
    selOp.appendChild(opt);
  }
  // Default to ">="
  selOp.value = ">=";

  // Value input
  const inpVal = document.createElement("input");
  inpVal.type      = "number";
  inpVal.className = "inp-value";
  inpVal.step      = "any";
  inpVal.placeholder = "value";
  inpVal.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });

  // Season type select
  const selSeason = document.createElement("select");
  selSeason.className = "sel-season";
  [["either", "Either"], ["regular", "Regular"], ["playoffs", "Playoff"]].forEach(([v, l]) => {
    const opt = document.createElement("option");
    opt.value       = v;
    opt.textContent = l;
    selSeason.appendChild(opt);
  });

  // Remove button
  const btnRemove = document.createElement("button");
  btnRemove.className = "btn-remove-filter";
  btnRemove.textContent = "✕";
  btnRemove.title = "Remove this filter";
  btnRemove.addEventListener("click", () => row.remove());

  row.appendChild(makeLabel("Stat"));
  row.appendChild(selField);
  row.appendChild(makeLabel("is"));
  row.appendChild(selOp);
  row.appendChild(inpVal);
  row.appendChild(makeLabel("in"));
  row.appendChild(selSeason);
  row.appendChild(btnRemove);

  document.getElementById("filter-rows").appendChild(row);
}

function makeLabel(text) {
  const lbl = document.createElement("label");
  lbl.textContent = text;
  return lbl;
}

function collectFilters() {
  const filters = [];
  for (const row of document.querySelectorAll(".filter-row")) {
    const field      = row.querySelector(".sel-field").value;
    const operator   = row.querySelector(".sel-operator").value;
    const rawVal     = row.querySelector(".inp-value").value.trim();
    const seasonType = row.querySelector(".sel-season").value;

    if (rawVal === "") continue;   // skip rows without a value
    const value = parseFloat(rawVal);
    if (isNaN(value)) continue;

    filters.push({ field, operator, value, season_type: seasonType });
  }
  return filters;
}

function clearAll() {
  document.getElementById("filter-rows").innerHTML = "";
  filterSerial = 0;
  allResults   = [];
  document.getElementById("results-summary").textContent = "";
  document.getElementById("table-body").innerHTML = "";
  document.getElementById("empty-msg").classList.add("hidden");
  addFilterRow();
}

/* ── Search ────────────────────────────────────────────────────── */
async function runSearch() {
  const filters = collectFilters();
  const spinner = document.getElementById("spinner");
  const msg     = document.getElementById("status-msg");

  spinner.classList.remove("hidden");
  msg.textContent = "Searching…";
  msg.className   = "update-msg";
  document.getElementById("btn-search").disabled = true;

  try {
    const data = await apiFetch("/api/filter_players", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ filters }),
    });

    allResults = data.results || [];
    msg.textContent = "";
    msg.className   = "update-msg";
    renderResults();
  } catch (err) {
    msg.textContent = `✗ ${err.message}`;
    msg.className   = "update-msg error";
  } finally {
    spinner.classList.add("hidden");
    document.getElementById("btn-search").disabled = false;
  }
}

/* ── Table headers ─────────────────────────────────────────────── */
function buildTableHeaders() {
  const headerRow  = document.getElementById("header-row");
  const subRow     = document.getElementById("subheader-row");
  headerRow.innerHTML = "";
  subRow.innerHTML    = "";

  for (const col of COLUMNS) {
    const th = document.createElement("th");
    th.textContent = col.label;
    th.dataset.key = col.key;
    if (!col.isName && !col.isSeason) {
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
  renderResults();
}

/* ── Render results ────────────────────────────────────────────── */
function renderResults() {
  const tbody    = document.getElementById("table-body");
  const emptyMsg = document.getElementById("empty-msg");
  const summary  = document.getElementById("results-summary");
  tbody.innerHTML = "";

  if (!allResults.length) {
    emptyMsg.classList.remove("hidden");
    summary.innerHTML = "No player-seasons matched your filters.";
    return;
  }
  emptyMsg.classList.add("hidden");
  summary.innerHTML = `<span class="match-count">${allResults.length}</span> player-season${allResults.length === 1 ? "" : "s"} matched`;

  const sorted = [...allResults].sort((a, b) => {
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

  for (const row of sorted) {
    tbody.appendChild(buildRow(row));
  }
}

function buildRow(player) {
  const tr = document.createElement("tr");

  for (const col of COLUMNS) {
    const td = document.createElement("td");

    if (col.isName) {
      td.className = "name-cell";
      const a = document.createElement("a");
      a.className  = "player-link";
      a.href       = `/player/${player.player_id}`;
      a.textContent = player.name;
      td.appendChild(a);

    } else if (col.isSeason) {
      // Season label + badge
      const span = document.createElement("span");
      span.textContent = player.season_label || "—";
      td.appendChild(span);
      const badge = document.createElement("span");
      badge.className   = "badge " + (player.season_type === "regular" ? "badge-regular" : "badge-playoffs");
      badge.textContent = player.season_type === "regular" ? "REG" : "PLY";
      badge.style.marginLeft = "6px";
      td.appendChild(badge);

    } else if (col.isKyle) {
      td.className = "kyle-cell";
      const norm = document.createElement("div");
      norm.className  = "norm-val";
      norm.textContent = fmt(player.kyle_rating);
      td.appendChild(norm);

    } else {
      const rawVal  = player[col.key];
      const normVal = player[col.key + "_norm"];

      if (normVal !== null && normVal !== undefined) {
        const norm = document.createElement("div");
        norm.className  = "norm-val " + colorClass(normVal);
        norm.textContent = fmt(normVal);

        // Asterisk indicator on on_off_diff if flagged
        if (col.key === "on_off_diff" && player.on_off_asterisk) {
          const ast = document.createElement("span");
          ast.className  = "asterisk-note";
          ast.title      = "On/off data unreliable; average of other norms used";
          ast.textContent = "*";
          norm.appendChild(ast);
        }

        td.appendChild(norm);
      }

      if (rawVal !== null && rawVal !== undefined) {
        const raw = document.createElement("div");
        raw.className  = "raw-val";
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
  if (key === "minutes")      return Math.round(n).toLocaleString();
  if (key === "points_per_shot") return n.toFixed(2);
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
