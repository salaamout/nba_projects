"""
Bulk-populate missing bbref_url values for players who have an nba_id but no bbref_url.
Uses the standard Basketball-Reference ID formula:
  first 5 chars of last name + first 2 chars of first name + "01"  (all lowercase, ASCII only)

Special cases are handled manually below.
"""
import sqlite3
import unicodedata
import re
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nba.db")

# Manual overrides for names that don't follow the standard formula or are ambiguous
OVERRIDES = {
    "Metta World Peace":  "artesro01",   # born Ron Artest; BBRef still uses Artest
    "Nenê":               "hilarnen01",  # full name Nene Hilario
    "Jermaine O'Neal":    "onealje01",
    "Peja Stojaković":    "stojape01",
    "Goran Dragić":       "dragigo01",
    "Manu Ginóbili":      "ginobma01",
    "Amar'e Stoudemire":  "stoudam01",
    "Shareef Abdur-Rahim":"abdursh01",
}


def to_ascii(s: str) -> str:
    """Strip accents and non-ASCII characters."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def bbref_id(full_name: str) -> str:
    """Derive the standard BBRef player ID from a full name."""
    name = to_ascii(full_name).lower()
    # Remove everything except letters and spaces
    name = re.sub(r"[^a-z ]", "", name)
    parts = name.split()
    if len(parts) < 2:
        raise ValueError(f"Cannot parse name: {full_name!r}")
    first = parts[0]
    last  = " ".join(parts[1:]).replace(" ", "")
    return last[:5] + first[:2] + "01"


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    rows = cur.execute(
        "SELECT name FROM players "
        "WHERE nba_id IS NOT NULL AND nba_id != '' "
        "  AND (bbref_url IS NULL OR bbref_url = '')"
    ).fetchall()

    updated = []
    skipped = []

    for (name,) in rows:
        if name in OVERRIDES:
            bid = OVERRIDES[name]
        else:
            try:
                bid = bbref_id(name)
            except ValueError as e:
                skipped.append((name, str(e)))
                continue

        url = f"/players/{bid[0]}/{bid}.html"
        cur.execute(
            "UPDATE players SET bbref_url = ? WHERE name = ?",
            (url, name),
        )
        updated.append((name, url))

    conn.commit()
    conn.close()

    print(f"\nUpdated {len(updated)} players:")
    for name, url in updated:
        print(f"  {name:30s}  →  {url}")

    if skipped:
        print(f"\nSkipped {len(skipped)} players:")
        for name, reason in skipped:
            print(f"  {name}: {reason}")


if __name__ == "__main__":
    main()
