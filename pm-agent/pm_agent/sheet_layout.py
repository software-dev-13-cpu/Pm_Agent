from .rules import parse_date

TABLE_FIRST_HEADER = "Date"


def parse_percent(value):
    """'45', '45%' or 45 -> 45.0. Returns None unless it is a number from 0 to 100."""
    try:
        number = float(str(value).replace("%", "").strip())
    except ValueError:
        return None
    return number if 0 <= number <= 100 else None


def parse_project_tab(title, gid, rows):
    """Splits one project tab into its details block and its progress rows."""
    info = {}
    header_index = None
    for i, row in enumerate(rows):
        first = row[0].strip() if row else ""
        if first == TABLE_FIRST_HEADER:
            header_index = i
            break
        if first:
            info[first] = row[1].strip() if len(row) > 1 else ""

    entries = []
    bad_rows = []
    if header_index is not None:
        for sheet_row, row in enumerate(rows[header_index + 1:], start=header_index + 2):
            if not any(cell.strip() for cell in row):
                continue
            padded = row + [""] * (5 - len(row))
            day = parse_date(padded[0])
            pct = parse_percent(padded[3])
            if day is None or pct is None:
                bad_rows.append(sheet_row)
                continue
            entries.append({"date": day, "developer": padded[1].strip(),
                            "work": padded[2].strip(), "pct": pct,
                            "blocker": padded[4].strip()})

    entries.sort(key=lambda e: e["date"])
    return {"tab": title, "gid": gid, "info": info, "entries": entries,
            "bad_rows": bad_rows, "has_table": header_index is not None}