"""Remember places across runs, so a later run doesn't return them again.

Two sources of "already seen":
  - a history file the app appends to after every run, and
  - spreadsheets from earlier runs, uploaded in the form. These matter on a
    free host, whose disk is wiped on restart, and for runs made elsewhere.

Each record keeps just the fields scraper._same_prospect matches on.
"""
import hashlib
import json
import os
import re
import threading
import datetime

from openpyxl import load_workbook

import config

FIELDS = ("name", "website", "address", "phone_maps", "email", "place_id")
_lock = threading.Lock()


def namespace(google_key=""):
    """Whose history this is.

    On a hosted copy, visitors bring their own keys and must not hide each
    other's results, so history is kept per Google key (hashed, never stored
    in clear). Runs on the shared server keys share one history.
    """
    if not google_key or google_key == config.GOOGLE_PLACES_API_KEY:
        return "shared"
    return hashlib.sha256(google_key.encode()).hexdigest()[:16]


def _path(ns):
    return os.path.join(config.HISTORY_DIR, f"seen_{ns}.json")


def load(ns):
    try:
        with open(_path(ns), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(ns, prospects, location=""):
    """Append this run's processed places to the history file."""
    today = datetime.date.today().isoformat()
    new = [{**{k: (p.get(k) or "") for k in FIELDS},
            "first_seen": today, "location": location} for p in prospects]
    if not new:
        return
    with _lock:
        os.makedirs(config.HISTORY_DIR, exist_ok=True)
        data = load(ns) + new
        tmp = _path(ns) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, _path(ns))          # never leave a half-written file


def clear(ns):
    with _lock:
        try:
            os.remove(_path(ns))
        except FileNotFoundError:
            pass


PHONE_LINE_RE = re.compile(r"\(Maps\)|maps", re.I)


def from_spreadsheet(fileobj):
    """Records from the Summary sheet of a spreadsheet this tool produced.

    Related Locations are included too: they were merged into a row, so
    finding them again next run would be the same duplicate.
    """
    wb = load_workbook(fileobj, read_only=True, data_only=True)
    ws = wb["Summary"] if "Summary" in wb.sheetnames else wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows, [])]
    col = {h: i for i, h in enumerate(header)}
    if "Organization" not in col:
        raise ValueError("not a lead spreadsheet (no Organization column)")

    def cell(row, name):
        i = col.get(name)
        return str(row[i] or "").strip() if i is not None and i < len(row) else ""

    out = []
    for row in rows:
        name = cell(row, "Organization")
        if not name:
            continue
        # Phone holds labeled lines ("(865) 555-0100 (website)"); the Maps
        # one is the comparable field, else take the first.
        phones = [ln for ln in cell(row, "Phone").splitlines() if ln.strip()]
        maps = next((ln for ln in phones if PHONE_LINE_RE.search(ln)), phones[0] if phones else "")
        out.append({"name": name, "website": cell(row, "Website"),
                    "address": cell(row, "Address").splitlines()[0] if cell(row, "Address") else "",
                    "phone_maps": maps, "email": cell(row, "Email"), "place_id": ""})
        for rel in re.split(r"[;\n]", cell(row, "Related Locations")):
            rel = re.sub(r"\s*\(.*?\)\s*$", "", rel).strip()
            if rel:
                out.append({"name": rel, "website": cell(row, "Website"),
                            "address": "", "phone_maps": "", "email": "", "place_id": ""})
    wb.close()
    return out
