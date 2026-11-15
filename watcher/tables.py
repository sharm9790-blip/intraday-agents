"""Phone-sized monospace tables for Telegram result messages (scorecard, expectancy,
weekly, my trades). Telegram has no real tables, but a <pre> block keeps columns lined
up; a phone shows ~32 characters across, so keep tables to 3–5 narrow columns.

Live alerts (BUY NOW, T1, SL, exit) stay as short lines: they are read in two seconds.
"""


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(x, plus=True):
    """Signed whole rupees without the ₹ sign, for table cells: +1,564 / -420 / 0."""
    if x is None:
        return "–"
    v = round(x)
    if v == 0:
        return "0"
    s = f"{abs(v):,}"
    return ("+" if v > 0 and plus else "-" if v < 0 else "") + s


def r(x):
    return "–" if x is None else f"{x:+.2f}"


def short(sym, n=10):
    sym = str(sym)
    return sym if len(sym) <= n else sym[:n - 1] + "…"


def table(header, rows, right=None, total=None):
    """header: list of column titles; rows: list of lists; right: set of column indexes
    to right-align (default: every column but the first). Returns ONE string, a <pre> block."""
    right = set(range(1, len(header))) if right is None else set(right)
    body = [list(map(str, header))] + [[str(c) for c in row] for row in rows]
    if total:
        body.append([str(c) for c in total])
    w = [max(len(row[i]) for row in body) for i in range(len(header))]

    def fmt(row):
        return " ".join(c.rjust(w[i]) if i in right else c.ljust(w[i]) for i, c in enumerate(row)).rstrip()

    out = [fmt(body[0])] + [fmt(x) for x in body[1:len(rows) + 1]]
    if total:
        out += ["─" * max(len(x) for x in out), fmt(body[-1])]
    return "<pre>" + _esc("\n".join(out)) + "</pre>"
