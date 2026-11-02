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
        con.execute("CREATE TABLE sheet AS SELECT * FROM read_csv_auto(?, header = true)", [path])
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


def _as_csv(columns, rows):
    """Rows as CSV text, cut at MAX_CHARS."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for i, row in enumerate(rows):
        writer.writerow([_cell(v) for v in row])
        if out.tell() > MAX_CHARS:
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
    columns = ", ".join(
        f'"{name}" ({_TYPES.get(kind, kind.lower())}' + (", % sign removed" if name in percent else "") + ")"
        for name, kind, *_ in info
    )
    where = " – ".join(p for p in (f'"{title}"' if title else "", f'tab "{tab}"' if tab else "") if p)
    return (
        f"Google Sheet {where}: {count} rows, in SQL table `sheet`.\n"
        f"Columns: {columns}\nFirst rows:\n{first}\n"
        "Use sheet_query with SQL to get the data you need (Google Sheets' grid is a picture)."
    )


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


def query(text, sql):
    """Run one read-only SQL statement on the sheet; the answer as CSV text."""
    sql = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql:
        raise ValueError("sheet_query takes one SELECT statement on table `sheet`.")
    con, _ = _load(text)
    try:
        cur = con.execute(sql)
        columns, rows = [d[0] for d in cur.description], cur.fetchall()
        # Top-N: never hide a tie. If the rewrite doesn't work for this query, keep the original answer.
        if (tied_sql := _with_ties(sql)) is not None:
            try:
                tied = con.execute(tied_sql).fetchall()
                if len(tied) > len(rows):
                    note = f"\n(includes {len(tied) - len(rows)} more row(s) tied with the last one – report them all)"
                    return _as_csv(columns, tied) + note
            except duckdb.Error:
                pass
        return _as_csv(columns, rows)
    except duckdb.Error as err:
        raise ValueError(f"SQL error: {str(err).splitlines()[0]}. Fix the query and try again.") from None
    finally:
        con.close()
