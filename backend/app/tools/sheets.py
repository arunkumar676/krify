"""Google Sheets: answer the AI's SQL questions about a sheet.

The extension downloads the sheet as CSV (with the user's own login); here it's loaded into an
in-memory DuckDB table called `sheet`. Only the answer rows go to the AI, never the whole sheet.
DuckDB can't touch files or the network here, so the AI's SQL can only read this table.
"""

import csv
import io
import os
import re
import tempfile

import duckdb

MAX_CHARS = 3000
_PERCENT = re.compile(r"^-?\d[\d,]*(\.\d+)?%$")
_GROUPED_NUMBER = re.compile(r"^-?\d{1,3}(,\d{2,3})+(\.\d+)?$")  # 1,200 or 12,34,567.5
_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_TYPES = {"VARCHAR": "text", "BIGINT": "number", "INTEGER": "number", "DOUBLE": "number",
          "DATE": "date", "TIMESTAMP": "date+time", "BOOLEAN": "yes/no", "TIME": "time"}


def _clean(text):
    """'83.6%' → 83.6 and '1,200' → 1200, for columns where every value looks like that,
    so the AI can compare them as numbers. Returns (csv text, names of percent columns)."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return text, set()
    header, body = rows[0], rows[1:]
    percent = set()
    for col in range(len(header)):
        values = [r[col].strip() for r in body if col < len(r) and r[col].strip()]
        if not values:
            continue
        if all(_PERCENT.match(v) for v in values):
            percent.add(header[col])
            for r in body:
                if col < len(r):
                    r[col] = r[col].strip().rstrip("%").replace(",", "")
        elif all(_GROUPED_NUMBER.match(v) or _PLAIN_NUMBER.match(v) for v in values) and any(
            _GROUPED_NUMBER.match(v) for v in values
        ):
            for r in body:
                if col < len(r):
                    r[col] = r[col].strip().replace(",", "")
    out = io.StringIO()
    csv.writer(out).writerows([header, *body])
    return out.getvalue(), percent


def _load(text):
    """CSV text → (DuckDB connection with table `sheet`, percent columns)."""
    text, percent = _clean(text)
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        con = duckdb.connect(":memory:")
        # _row = the row's number in the sheet (row 1 holds the column names), for go_to_cell.
        con.execute(
            "CREATE TABLE sheet AS SELECT ROW_NUMBER() OVER () + 1 AS _row, * FROM read_csv_auto(?, header = true)",
            [path],
        )
    finally:
        os.remove(path)
    # From here on the AI's SQL can't read files or the network, and can't switch that back on.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con, percent


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        value = round(value, 2)
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _as_csv(columns, rows, unlimited=False):
    """Rows as CSV text, cut at MAX_CHARS (not in Unrestricted mode: `unlimited`)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for i, row in enumerate(rows):
        writer.writerow([_cell(v) for v in row])
        if not unlimited and out.tell() > MAX_CHARS:
            return out.getvalue() + f"…({len(rows) - i - 1} more rows not shown – ask for fewer rows or columns)"
    if not rows:
        writer.writerow(["(no matching rows)"])
    return out.getvalue().rstrip("\n")


def summary(text, title="", tab=""):
    """What the AI needs to write SQL: columns with types, row count, first rows."""
    con, percent = _load(text)
    try:
        info = con.execute("DESCRIBE sheet").fetchall()  # (name, type, ...)
        count = con.execute("SELECT COUNT(*) FROM sheet").fetchone()[0]
        cur = con.execute("SELECT * FROM sheet LIMIT 3")
        first = _as_csv([d[0] for d in cur.description], cur.fetchall())
    finally:
        con.close()
    # The sheet's own column letters (A = its first column; _row was added by Krify, it has none),
    # so the AI never counts columns to build a cell address.
    columns = ", ".join(
        f'"{name}" ({_TYPES.get(kind, kind.lower())}, column {_letter(i)}'
        + (", % sign removed" if name in percent else "") + ")"
        for i, (name, kind, *_) in enumerate((c for c in info if c[0] != "_row"), 1)
    )
    where = " – ".join(p for p in (f'"{title}"' if title else "", f'tab "{tab}"' if tab else "") if p)
    return (
        f"Google Sheet {where}: {count} rows, in SQL table `sheet`.\n"
        f"Columns: {columns}\n"
        '"_row" is the row number in the sheet (not a sheet column). Cell address = column letter + _row '
        "(e.g. column B, _row 22 → B22).\n"
        f"First empty row (where a new row goes): {first_empty_row(text)}.\n"
        f"First rows:\n{first}\n"
        "Use sheet_query with SQL to get the data you need (Google Sheets' grid is a picture)."
    )


_CELL = re.compile(r"^([A-Z]{1,3})(\d{1,7})$")
MAX_CELLS = 60


def _letter(n):
    """Column number → letters, like in Sheets: 1 → A, 27 → AA."""
    name = ""
    while n:
        n, rest = divmod(n - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell_pos(ref):
    """'B12' → (row 12, column 2), both counted from 1 like in Sheets."""
    match = _CELL.match(ref)
    if not match:
        raise ValueError(f'"{ref}" isn\'t a cell like A130.')
    col = 0
    for letter in match[1]:
        col = col * 26 + ord(letter) - 64
    return int(match[2]), col


def first_empty_row(text):
    """The sheet row number just after the last row that has any data (where a new row goes)."""
    rows = list(csv.reader(io.StringIO(text)))
    last = max((i for i, row in enumerate(rows, 1) if any(cell.strip() for cell in row)), default=0)
    return last + 1


def filled_cells(text, start, typed):
    """Cells that typing `typed` at `start` would overwrite: [(address, current value)].
    \\t moves one column right, \\n starts the next row (back at the start column, as Sheets does);
    an empty piece types nothing, so that cell isn't counted."""
    typed = typed.replace("\\n", "\n").replace("\\t", "\t")
    r0, c0 = _cell_pos(start)
    rows = list(csv.reader(io.StringIO(text)))
    found = []
    for i, line in enumerate(typed.split("\n")):
        for j, value in enumerate(line.split("\t")):
            if not value:
                continue
            r, c = r0 + i, c0 + j
            row = rows[r - 1] if r - 1 < len(rows) else []
            current = row[c - 1] if c - 1 < len(row) else ""
            if current.strip():
                found.append((f"{_letter(c)}{r}", current))
    return found


def cells(text, ref, unlimited=False):
    """Exact cell values by address (A130, or A128:C130), read from the sheet as it is now –
    used to check an edit landed in the right place."""
    ref = (ref or "").strip().upper().replace("$", "")
    start, _, end = ref.partition(":")
    (r1, c1), (r2, c2) = _cell_pos(start), _cell_pos(end or start)
    r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
    if not unlimited and (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_CELLS:
        raise ValueError(f"Too many cells – read at most {MAX_CELLS} at once.")
    rows = list(csv.reader(io.StringIO(text)))
    lines = []
    for r in range(r1, r2 + 1):
        row = rows[r - 1] if r - 1 < len(rows) else []
        for c in range(c1, c2 + 1):
            name = _letter(c)
            value = row[c - 1] if c - 1 < len(row) else ""
            lines.append(f"{name}{r}: {value!r}" if value else f"{name}{r}: (empty)")
    return "\n".join(lines)


# "... ORDER BY x DESC LIMIT 1" at the end of a query: top-N questions ("who scored highest").
_TOP_N = re.compile(r"(?is)^(?P<base>.*)\border\s+by\s+(?P<order>(?:(?!\border\s+by\b).)+?)\s+limit\s+(?P<n>\d+)\s*$")


def _with_ties(sql):
    """Same top-N query, but also keeping rows tied with the last one (two students on 100),
    or None if the query isn't a simple top-N."""
    match = _TOP_N.match(sql)
    if not match or re.search(r"(?i)\bqualify\b", match["base"]):
        return None
    base, order, n = match["base"].rstrip(), match["order"], match["n"]
    return f"{base} QUALIFY RANK() OVER (ORDER BY {order}) <= {n} ORDER BY {order}"


def query(text, sql, unlimited=False):
    """Run one read-only SQL statement on the sheet; the answer as CSV text (whole in Unrestricted
    mode: `unlimited`)."""
    sql = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql:
        raise ValueError("sheet_query takes one SELECT statement on table `sheet`.")
    con, _ = _load(text)
    try:
        cur = con.execute(sql)
        columns, rows = [d[0] for d in cur.description], cur.fetchall()
        # Which sheet column each answer column is, e.g. "First Name" = column B (no counting needed).
        sheet_cols = [name for name, *_ in con.execute("DESCRIBE sheet").fetchall() if name != "_row"]
        letters = [f'"{c}" = column {_letter(sheet_cols.index(c) + 1)}' for c in columns if c in sheet_cols]
        if not letters and "_row" in columns:  # only row numbers asked: give every column's letter
            letters = [f'"{c}" = {_letter(i)}' for i, c in enumerate(sheet_cols if unlimited else sheet_cols[:30], 1)]
        where = f"\n(in the sheet: {', '.join(letters)}; cell = letter + _row)" if letters else ""
        # Top-N: never hide a tie. If the rewrite doesn't work for this query, keep the original answer.
        if (tied_sql := _with_ties(sql)) is not None:
            try:
                tied = con.execute(tied_sql).fetchall()
                if len(tied) > len(rows):
                    note = f"\n(includes {len(tied) - len(rows)} more row(s) tied with the last one – report them all)"
                    return _as_csv(columns, tied, unlimited) + note + where
            except duckdb.Error:
                pass
        return _as_csv(columns, rows, unlimited) + where
    except duckdb.Error as err:
        raise ValueError(f"SQL error: {str(err).splitlines()[0]}. Fix the query and try again.") from None
    finally:
        con.close()
