/* ── State ─────────────────────────────────────────────────────── */
let tableData = [];
let sortCol = "total_kyle";
let sortDir = "desc";

/* ── Column definitions ────────────────────────────────────────── */
const COLUMNS = [
  { key: "name",          label: "Player",          isName: true  },
  { key: "regular_kyle",  label: "Regular K.Y.L.E.", isKyle: true  },
  { key: "playoffs_kyle", label: "Playoffs K.Y.L.E.", isKyle: true },
  { key: "total_kyle",    label: "Total K.Y.L.E.",   isTotal: true },
];

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", async () => {
  buildTableHeaders();
  await loadData();
});

/* ── Fetch ─────────────────────────────────────────────────────── */
async function loadData() {
  const spinner  = document.getElementById("spinner");
  const msg      = document.getElementById("update-msg");
  spinner.classList.remove("hidden");
  msg.textContent = "Loading…";
  msg.className   = "update-msg";

  try {
    tableData = await apiFetch("/api/cumulative_kyle");
    msg.textContent = `${tableData.length} player${tableData.length === 1 ? "" : "s"}`;
    msg.className   = "update-msg success";
    renderTable();
  } catch (err) {
    msg.textContent = `✗ ${err.message}`;
    msg.className   = "update-msg error";
  } finally {
    spinner.classList.add("hidden");
  }
}

/* ── Table headers ─────────────────────────────────────────────── */
function buildTableHeaders() {
  const headerRow = document.getElementById("header-row");
  headerRow.innerHTML = "";

  for (const col of COLUMNS) {
    const th = document.createElement("th");
    th.textContent  = col.label;
    th.dataset.key  = col.key;
    if (!col.isName) {
      th.addEventListener("click", () => handleSort(col.key));
    }
    if (col.key === sortCol) th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
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

  for (const player of sorted) {
    tbody.appendChild(buildRow(player));
  }
}

function buildRow(player) {
  const tr = document.createElement("tr");

  for (const col of COLUMNS) {
    const td = document.createElement("td");

    if (col.isName) {
      td.className = "name-cell";
      const a = document.createElement("a");
      a.className = "player-link";
      a.href = `/player/${player.player_id}`;
      a.textContent = player.name;
      td.appendChild(a);

    } else if (col.isTotal) {
      td.className = "kyle-cell cumulative-total";
      const div = document.createElement("div");
      div.className   = "norm-val";
      div.textContent = fmt(player.total_kyle);
      td.appendChild(div);

    } else {
      /* regular_kyle / playoffs_kyle */
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
  if (val === null || val === undefined) return "—";
  return parseFloat(val).toFixed(2);
}
