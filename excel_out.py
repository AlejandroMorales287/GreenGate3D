"""Build the GreenGate3D lead .xlsx.

Column design:
  - Contact precedence is website-first, Maps-second, with BOTH phones kept —
    "Phone" holds both, each on its own labeled line (like "Last Verified").
  - People are summarized into one "Other Contacts" cell (name, role, email, phone,
    LinkedIn, and why they matter) instead of five Top-Contact columns. Any
    other emails found on the site (not tied to a named person) are appended
    to that same cell instead of living in a separate column.
  - "Notes" is intentionally empty — it's Rich's column to write in.
  - "Last Verified" holds both dates in one cell (Maps pull vs. site crawl,
    each on its own line; a line is omitted when that source wasn't used).
"""
import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial")
HEADER_FILL = PatternFill("solid", fgColor="2E7D32")  # green header
NOTES_FILL = PatternFill("solid", fgColor="FFF9E6")   # Rich's column, visibly blank
WRAP = Alignment(vertical="top", wrap_text=True)
THIN = Border(*(Side(style="thin", color="DDDDDD"),) * 4)

SUMMARY_HEADERS = [
    "Organization", "Related Locations", "City", "Priority", "Type", "Address",
    "Phone", "Email", "Other Contacts", "All Emails (CC)", "Website",
    "Equipment Highlights", "3D Printer Models", "Why GreenGate3D",
    "Hours", "Notes", "Found Via", "Last Verified",
]
NOTES_COL = SUMMARY_HEADERS.index("Notes") + 1
PEOPLE_COL = SUMMARY_HEADERS.index("Other Contacts") + 1

CONTACT_HEADERS = ["Organization", "City", "Name", "Role", "Email", "Phone",
                   "LinkedIn", "Why This Person", "Source"]
ACTION_HEADERS = ["Rank", "Target", "City", "Priority", "Action", "Who to Contact",
                  "Contact Method", "Why This Is #1-10", "Expected Outcome"]

PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
# Roles that indicate purchasing/operations influence, for ranking people.
DECISION_WORDS = ("director", "president", "owner", "founder", "manager", "chief",
                  "head", "lead", "coordinator", "purchasing", "operations",
                  "facilities", "shop", "technical")


def _style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _person_rank(person):
    """Decision-makers first, then people we can actually reach."""
    role = (person.get("role") or "").lower()
    return (
        0 if any(w in role for w in DECISION_WORDS) else 1,
        0 if (person.get("email") or person.get("phone")) else 1,
    )


def sorted_people(lead):
    return sorted(lead.get("contacts") or [], key=_person_rank)


def format_people(lead):
    """One cell summarizing every known person at the org, plus any other
    emails found on the site that weren't tied to a named person.

    Each named block: "Name — Role" / contact details / why they matter.
    """
    people = sorted_people(lead)
    blocks = []
    for p in people:
        head = p.get("name", "").strip()
        if p.get("role"):
            head += f" — {p['role']}"
        lines = [head]
        reach = [v for v in (p.get("email"), p.get("phone"), p.get("linkedin")) if v]
        if reach:
            lines.append("   " + " | ".join(reach))
        if p.get("reason"):
            lines.append(f"   Why: {p['reason']}")
        blocks.append("\n".join(lines))
    other_emails = lead.get("other_emails") or []
    if other_emails:
        blocks.append("Other\n   " + " | ".join(other_emails))
    return "\n\n".join(blocks)


def format_all_emails(lead):
    """Every email for the lead in one paste-ready string for an email's CC line.

    Primary email first, then each person's, then other site emails; deduped
    case-insensitively. Semicolon-separated, which Outlook and Gmail both accept.
    """
    seen, out = set(), []
    candidates = [lead.get("email")]
    candidates += [p.get("email") for p in sorted_people(lead)]
    candidates += lead.get("other_emails") or []
    for e in candidates:
        e = (e or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return "; ".join(out)


def format_last_verified(lead):
    """One cell for both verification dates, each labeled on its own line."""
    lines = []
    if lead.get("verified_maps"):
        lines.append(f"{lead['verified_maps']} (Maps)")
    if lead.get("verified_web"):
        lines.append(f"{lead['verified_web']} (Website)")
    return "\n".join(lines)


def format_phone(lead):
    """One cell for both phone numbers, each labeled on its own line."""
    lines = []
    if lead.get("phone_web"):
        lines.append(f"{lead['phone_web']} (Website)")
    if lead.get("phone_maps"):
        lines.append(f"{lead['phone_maps']} (Maps)")
    return "\n".join(lines)


def format_source(lead):
    """How the lead was found, where its website came from, and which fields
    came from a web search rather than the company's own site.

    Places-only discovery misses anything without a Maps listing, and a value
    found by search is weaker evidence than one read off the company's site —
    both belong in front of whoever makes the call.
    """
    found = lead.get("discovered_via") or "Unknown"
    site_src = lead.get("website_source") or ""
    out = found
    if site_src and site_src != found:
        out = f"{found} (site via {site_src.lower()})"
    if lead.get("enriched"):
        out += f"; {lead['enriched']} via web search"
    return out


def _rank_key(lead):
    """Top-10 order: derived priority, then reachability, then review count."""
    return (
        PRIORITY_ORDER.get(lead.get("priority"), 3),
        0 if lead.get("contacts") else 1,
        0 if (lead.get("email") or lead.get("phone_web") or lead.get("phone_maps")) else 1,
        -(lead.get("reviews") or 0),
    )


def _action(lead):
    if lead.get("email"):
        return f"Email {lead['email']}"
    phone = lead.get("phone_web") or lead.get("phone_maps")
    if phone:
        return f"Call {phone}"
    if lead.get("website"):
        return f"Contact via {lead['website']}"
    return "Research contact details"


def _who_to_contact(lead):
    people = sorted_people(lead)
    if people:
        top = people[0]
        role = f" ({top['role']})" if top.get("role") else ""
        return f"{top.get('name', '')}{role}"
    return lead.get("email") or lead.get("phone_web") or lead.get("phone_maps") or ""


def _why_ranked(lead):
    bits = [f"{lead.get('priority', 'LOW')} priority"]
    if lead.get("printer_models"):
        bits.append(f"printers on site: {lead['printer_models']}")
    elif lead.get("equipment_highlights"):
        bits.append("3D/maker equipment listed")
    people = sorted_people(lead)
    if people:
        bits.append(f"{len(people)} named contact(s), top: {people[0].get('name', '')}")
    if lead.get("email"):
        bits.append("direct email")
    elif lead.get("phone_web") or lead.get("phone_maps"):
        bits.append("phone reachable")
    if (lead.get("reviews") or 0) >= 20:
        bits.append(f"{lead['reviews']} reviews")
    if lead.get("related"):
        bits.append(f"covers {len(lead['related']) + 1} locations")
    return "; ".join(bits)


def _expected_outcome(lead):
    if lead.get("printer_models") or lead.get("equipment_highlights"):
        return "Gauge interest in a recycled-PETG spool trial for their printers"
    return "Confirm whether they run 3D printers and gauge filament interest"


def build(leads, out_path):
    wb = Workbook()

    # --- Summary ---
    ws = wb.active
    ws.title = "Summary"
    ws.append(SUMMARY_HEADERS)
    for L in leads:
        related = "; ".join(L.get("related") or [])
        ws.append([
            L.get("name", ""), related, L.get("city", ""), L.get("priority", ""),
            L.get("type", ""), L.get("address", ""),
            format_phone(L),
            L.get("email", ""), format_people(L), format_all_emails(L),
            L.get("website", ""),
            L.get("equipment_highlights", ""), L.get("printer_models", ""),
            L.get("pitch", ""), L.get("hours", ""),
            "",  # Notes — left blank for Rich
            format_source(L),
            format_last_verified(L),
        ])

    # --- All Contacts (flat list of every named person) ---
    wc = wb.create_sheet("All Contacts")
    wc.append(CONTACT_HEADERS)
    for L in leads:
        for p in sorted_people(L):
            wc.append([L.get("name", ""), L.get("city", ""), p.get("name", ""),
                       p.get("role", ""), p.get("email", ""), p.get("phone", ""),
                       p.get("linkedin", ""), p.get("reason", ""),
                       "Company website"])
        if not L.get("contacts") and L.get("email"):
            wc.append([L.get("name", ""), L.get("city", ""), "",
                       "General/unverified inbox", L.get("email", ""),
                       L.get("phone_web") or L.get("phone_maps", ""),
                       L.get("org_linkedin", ""), "", "Company website"])

    # --- Top 10 Actions ---
    wa = wb.create_sheet("Top 10 Actions")
    wa.append(ACTION_HEADERS)
    for i, L in enumerate(sorted(leads, key=_rank_key)[:10], 1):
        if L.get("email"):
            method = "Email"
        elif L.get("phone_web") or L.get("phone_maps"):
            method = "Phone"
        else:
            method = "Website"
        wa.append([
            i, L.get("name", ""), L.get("city", ""), L.get("priority", ""),
            _action(L), _who_to_contact(L), method, _why_ranked(L),
            _expected_outcome(L),
        ])

    # --- styling + widths ---
    widths = {"Summary": 22, "All Contacts": 20, "Top 10 Actions": 18}
    for sheet in wb.worksheets:
        ncols = sheet.max_column
        _style_header(sheet, ncols)
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.font = BODY_FONT
                cell.alignment = WRAP
                cell.border = THIN
        for c in range(1, ncols + 1):
            sheet.column_dimensions[sheet.cell(1, c).column_letter].width = widths[sheet.title]

    # Other Contacts holds multi-line blocks; Notes is Rich's writing space.
    ws.column_dimensions[ws.cell(1, PEOPLE_COL).column_letter].width = 52
    ws.column_dimensions[ws.cell(1, NOTES_COL).column_letter].width = 30
    for row in range(2, ws.max_row + 1):
        ws.cell(row, NOTES_COL).fill = NOTES_FILL

    wb.save(out_path)
    return out_path
