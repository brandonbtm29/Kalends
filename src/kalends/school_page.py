"""Read an academic calendar that a school publishes as a web page with a table of dates.

Many schools (the University of Akron among them) don't offer a calendar feed, only
a page like::

    Term   | Event                  | Career | 2026-2027    | 2027-2028
    Fall   | Start of term          | G/U    | Aug 24, 2026 | Aug 23, 2027
    Fall   | Thanksgiving recess    | G/U    | Nov 26-29, 2026 | Nov 25-28, 2027

This module turns tables like that into :class:`Row` objects: one per event and
year column, with the dates read from the cell. It copes with the common layouts
(a Term column, a heading or a spanning row per term, one or several date columns,
columns named after academic years or terms) and with obvious year typos: a Spring
2027 row in the 2026-2027 column that says "May 31, 2026" is read as 2027 and flagged.

Only the standard library is used, so nothing new has to be installed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

MAX_PAGE = 5_000_000  # characters; a calendar page is far smaller than this

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_ORD = r"(?:st|nd|rd|th)?"
_MENTION = re.compile(
    rf"(?P<iso>\b(?P<iy>\d{{4}})-(?P<im>\d{{1,2}})-(?P<id>\d{{1,2}})\b)"
    rf"|(?P<num>\b(?P<nm>\d{{1,2}})/(?P<nd>\d{{1,2}})(?:/(?P<ny>\d{{4}}|\d{{2}}))?\b)"
    rf"|(?P<md>\b(?P<mm>{_MON})\.?\s+(?P<mdd>\d{{1,2}}){_ORD}\b(?:\s*,?\s*(?P<my>\d{{4}})\b)?)"
    rf"|(?P<dm>\b(?P<dd>\d{{1,2}}){_ORD}\s+(?P<dmm>{_MON})\b\.?(?:\s*,?\s*(?P<dy>\d{{4}})\b)?)"
    rf"|(?P<bare>-\s*(?P<bd>\d{{1,2}}){_ORD}\b(?!\s*/)(?!{_ORD}\s+{_MON}\b)(?:\s*,?\s*(?P<by>\d{{4}})\b)?)",
    re.I,
)
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|nesday|rsday|urday|sday)?\b\.?,?", re.I
)
_DASHES = re.compile(r"\s*(?:[–—‒−]|\bto\b|\bthrough\b|\bthru\b|\buntil\b)\s*", re.I)
_ACAD = re.compile(r"\b(20\d\d)\s*[-–—/]\s*(20\d\d|\d\d)\b")
_YEAR = re.compile(r"\b(20\d\d)\b")
SEASON = re.compile(r"\b(fall|autumn|spring|summer|winter)\b", re.I)


@dataclass
class When:
    first: date
    last: date
    fixed_from: str | None = None  # the cell's own text, when a likely year typo was corrected


@dataclass
class Row:
    """One date read from the page: an event in one year column."""

    key: str  # stable id for this row and column (used to spot changes between visits)
    ignore_key: str  # what "Ignore this row" remembers: the same event in every year
    title: str
    group: str | None = None  # the Term column, a spanning row or a heading: "Fall"
    column: str | None = None  # the date column's heading: "2026-2027"
    career: str | None = None  # "G", "U", "G/U" (shown in the review, not used)
    raw: str = ""  # the date as written
    first: date | None = None
    last: date | None = None
    fixed_from: str | None = None
    acad: tuple[int, int] | None = None  # academic year of the column, if it names one
    year_hint: int | None = None  # a single year from the column or group, if any
    role: str = ""  # filled in when terms are built: start, end, exams, break, closed, ...
    term: str | None = None  # the term this row ended up in


def expected_year(
    month: int, acad: tuple[int, int] | None, year: int | None, season: str | None
) -> int | None:
    """The year a date in this month most likely belongs to, given the column and group."""
    s = (season or "").lower()
    if acad:
        y1, y2 = acad
        if s in ("fall", "autumn"):
            return y1
        if s in ("spring", "summer"):
            return y2
        return y1 if month >= 7 else y2
    return year


def _y(text: str | None) -> int | None:
    if not text:
        return None
    n = int(text)
    return n + 2000 if n < 100 else n


def parse_when(
    text: str,
    expect: Callable[[int], int | None] | None = None,
    fix: bool = False,
) -> When | None:
    """First and last day written in a cell ("Oct 12-18, 2026", "Mar 1-Mar 7, 2027", "8/24/2026").

    ``expect(month)`` gives the year to use when the cell doesn't say; with ``fix``, a year
    that's one off from what's expected is corrected (and reported in ``fixed_from``).
    Returns None for cells without a day in them ("TBD", "Mid-March").
    """
    t = _WEEKDAY.sub(" ", text.replace("\xa0", " "))
    t = _DASHES.sub(" - ", t)
    t = re.sub(r"\s+", " ", t).strip()
    found: list[list] = []  # [month, day, year or None]
    for m in _MENTION.finditer(t):
        if m.group("iso"):
            found.append([int(m.group("im")), int(m.group("id")), int(m.group("iy"))])
        elif m.group("num"):
            found.append([int(m.group("nm")), int(m.group("nd")), _y(m.group("ny"))])
        elif m.group("md"):
            found.append([MONTHS[m.group("mm").lower()], int(m.group("mdd")), _y(m.group("my"))])
        elif m.group("dm"):
            found.append([MONTHS[m.group("dmm").lower()], int(m.group("dd")), _y(m.group("dy"))])
        elif m.group("bare") and found:
            found.append([found[-1][0], int(m.group("bd")), _y(m.group("by"))])
    if not found:
        return None
    a, b = list(found[0]), list(found[-1])  # copies: with one date, both start out the same
    # A range written with one year ("Nov 26 - Dec 1, 2026") shares it; Dec-Jan ranges straddle it.
    if a[2] is None and b[2] is not None:
        a[2] = b[2] - 1 if a[0] > b[0] else b[2]
    if b[2] is None and a[2] is not None:
        b[2] = a[2] + 1 if b[0] < a[0] else a[2]
    fixed = None
    if a[2] is None:
        want = expect(a[0]) if expect else None
        if want is None:
            return None
        a[2] = want
        b[2] = want + 1 if b[0] < a[0] else want
    elif fix and expect:
        want = expect(a[0])
        if want is not None and abs(a[2] - want) == 1:
            shift = want - a[2]
            a[2] += shift
            b[2] += shift
            fixed = text.strip()
    try:
        first = date(a[2], a[0], a[1])
        last = date(b[2], b[0], b[1])
    except ValueError:
        return None
    if last < first or (last - first).days > 150:
        last = first  # not a sensible range; keep the first day only
    return When(first, last, fixed)


# -- reading the HTML ----------------------------------------------------------------------------


@dataclass
class _Cell:
    text: str
    header: bool
    colspan: int = 1
    rowspan: int = 1


@dataclass
class _Table:
    heading: str  # the nearest heading or caption before the table
    rows: list[list[_Cell]]


class _Collector(HTMLParser):
    """Collects every table's cells, and the heading just before each table."""

    _HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6", "caption")
    _SKIP = ("script", "style", "noscript", "template", "svg")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.title = ""
        self._stack: list[_Table] = []  # open tables (nested ones are read on their own)
        self._row: list[list[_Cell]] = []  # the open row of each open table
        self._cell: list[_Cell | None] = []
        self._buf: list[list[str]] = []
        self._heading: list[str] | None = None
        self._last_heading = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in self._HEADINGS:
            self._heading = []
        elif tag == "table":
            self._stack.append(_Table(self._last_heading, []))
            self._row.append([])
            self._cell.append(None)
            self._buf.append([])
        elif self._stack and tag == "tr":
            self._end_cell()
            self._end_row()
        elif self._stack and tag in ("td", "th"):
            self._end_cell()

            def span(name: str) -> int:
                try:
                    return max(1, min(50, int(str(a.get(name) or "1").strip())))
                except ValueError:
                    return 1

            self._cell[-1] = _Cell("", tag == "th", span("colspan"), span("rowspan"))
            self._buf[-1] = []
        elif tag == "br" or tag in ("p", "div", "li"):
            self._text(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in self._HEADINGS and self._heading is not None:
            text = re.sub(r"\s+", " ", "".join(self._heading)).strip()
            if text:
                self._last_heading = text
                if tag == "caption" and self._stack:
                    self._stack[-1].heading = text
            self._heading = None
        elif self._stack and tag in ("td", "th"):
            self._end_cell()
        elif self._stack and tag == "tr":
            self._end_cell()
            self._end_row()
        elif self._stack and tag == "table":
            self._end_cell()
            self._end_row()
            self.tables.append(self._stack.pop())
            self._row.pop()
            self._cell.pop()
            self._buf.pop()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._heading is not None:
            self._heading.append(data)
        self._text(data)

    def _text(self, data: str) -> None:
        if self._stack and self._cell[-1] is not None:
            self._buf[-1].append(data)

    def _end_cell(self) -> None:
        if not self._stack or self._cell[-1] is None:
            return
        cell = self._cell[-1]
        cell.text = re.sub(r"\s+", " ", "".join(self._buf[-1]).replace("\xa0", " ")).strip()
        self._row[-1].append(cell)
        self._cell[-1] = None
        self._buf[-1] = []

    def _end_row(self) -> None:
        if self._stack and self._row[-1]:
            self._stack[-1].rows.append(self._row[-1])
            self._row[-1] = []


def _grid(table: _Table) -> list[list[_Cell]]:
    """Rows with colspans and rowspans filled in, so every row has a cell per column."""
    grid: list[list[_Cell]] = []
    pending: dict[int, tuple[_Cell, int]] = {}  # column -> (cell, rows still to fill)
    for row in table.rows:
        out: list[_Cell] = []
        cells = iter(row)
        col = 0
        while True:
            if col in pending:
                cell, left = pending[col]
                out.append(cell)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (cell, left - 1)
                col += 1
                continue
            cell = next(cells, None)
            if cell is None:
                break
            for i in range(cell.colspan):
                out.append(cell)
                if cell.rowspan > 1:
                    pending[col + i] = (cell, cell.rowspan - 1)
            col += cell.colspan
        while col in pending:  # spans reaching past the last written cell
            cell, left = pending.pop(col)
            out.append(cell)
            if left > 1:
                pending[col] = (cell, left - 1)
            col += 1
        grid.append(out)
    return grid


_EVENT_HEAD = re.compile(
    r"\b(event|events|description|activity|item|deadline|what|details?|title|name)\b", re.I
)
_DATE_HEAD = re.compile(r"\b(dates?|when|day|days|deadline date)\b", re.I)
_TERM_HEAD = re.compile(r"^(term|terms|semester|semesters|session|sessions|quarter|quarters)$", re.I)
_CAREER_HEAD = re.compile(r"\b(career|level|student|students|program|audience)\b", re.I)
_ANY_DATE = re.compile(
    rf"\b{_MON}\.?\s+\d{{1,2}}\b|\b\d{{1,2}}{_ORD}\s+{_MON}\b|\b\d{{1,2}}/\d{{1,2}}\b|\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b"
    r"|\bTBD\b|\bTBA\b",
    re.I,
)


def _column_kinds(header: list[str], body: list[list[str]]) -> dict[str, list[int] | int | None]:
    n = len(header)

    def share_dated(i: int) -> float:
        vals = [r[i] for r in body if i < len(r) and r[i]]
        return sum(1 for v in vals if _ANY_DATE.search(v)) / len(vals) if vals else 0.0

    dates = [
        i
        for i, h in enumerate(header)
        if (_ACAD.search(h) or _YEAR.search(h) or _DATE_HEAD.search(h)) and share_dated(i) >= 0.3
    ]
    if not dates:
        dates = [i for i in range(n) if share_dated(i) >= 0.5]
    rest = [i for i in range(n) if i not in dates]
    term = next((i for i in rest if _TERM_HEAD.match(header[i].strip())), None)
    career = next((i for i in rest if i != term and _CAREER_HEAD.search(header[i])), None)
    others = [i for i in rest if i not in (term, career)]
    event = next((i for i in others if _EVENT_HEAD.search(header[i])), None)
    if event is None and others:

        def avg(i: int) -> float:
            vals = [len(r[i]) for r in body if i < len(r)]
            return sum(vals) / len(vals) if vals else 0

        event = max(others, key=avg)
    return {"dates": dates, "term": term, "career": career, "event": event}


def _is_section_row(cells: list[_Cell]) -> str | None:
    """A row that is just a label spanning the table ("Fall Semester 2026"), if it is one."""
    texts = {c.text for c in cells if c.text}
    distinct = {id(c) for c in cells if c.text}
    if len(distinct) == 1 and len(texts) == 1:
        text = texts.pop()
        if len(cells) > 1 and not _ANY_DATE.search(text) and len(text) <= 80:
            return text
    return None


def read_tables(text: str) -> tuple[list[Row], str]:
    """Every dated row in the page's tables, and the page's title."""
    parser = _Collector()
    parser.feed(text[:MAX_PAGE])
    parser.close()
    rows: list[Row] = []
    seen: dict[str, int] = {}
    for table in parser.tables:
        grid = _grid(table)
        if len(grid) < 2:
            continue
        head_at = next((i for i, r in enumerate(grid[:5]) if r and all(c.header for c in r)), None)
        if head_at is None:
            head_at = next((i for i, r in enumerate(grid[:5]) if any(c.header for c in r)), 0)
        header = [c.text for c in grid[head_at]]
        body_cells = grid[head_at + 1 :]
        body = [[c.text for c in r] for r in body_cells]
        kinds = _column_kinds(header, body)
        dates, event = kinds["dates"], kinds["event"]
        if not dates or event is None:
            continue
        term_col, career_col = kinds["term"], kinds["career"]
        table_group = table.heading if SEASON.search(table.heading or "") else None
        table_acad = _ACAD.search(table.heading or "")
        table_year = _YEAR.search(table.heading or "")
        section: str | None = None
        for cells, values in zip(body_cells, body, strict=True):
            label = _is_section_row(cells)
            if label is not None:
                section = label
                continue
            if event >= len(values) or not values[event]:
                continue
            title = values[event]
            if term_col is not None and term_col < len(values):
                group = values[term_col] or None
            else:
                group = section or table_group
            career = values[career_col] if career_col is not None and career_col < len(values) else None
            for ci in dates:
                if ci >= len(values):
                    continue
                cell = values[ci]
                if not cell:
                    continue
                head = header[ci] if ci < len(header) else ""
                acad_m = _ACAD.search(head) or (None if _YEAR.search(head) else table_acad)
                acad = None
                if acad_m:
                    y1 = int(acad_m.group(1))
                    y2s = acad_m.group(2)
                    y2 = int(y2s) if len(y2s) == 4 else (y1 // 100) * 100 + int(y2s)
                    if y2 == y1 + 1:
                        acad = (y1, y2)
                year_hint = None
                if acad is None:
                    ym = _YEAR.search(head) or _YEAR.search(group or "") or table_year
                    year_hint = int(ym.group(1)) if ym else None
                season_m = SEASON.search(group or "") or SEASON.search(head)
                season = season_m.group(1) if season_m else None

                def expect(month, _a=acad, _y=year_hint, _s=season):
                    return expected_year(month, _a, _y, _s)

                # Correct one-off years only when the column says which academic year it is.
                when = parse_when(cell, expect, fix=acad is not None)
                column = head if (acad or _YEAR.search(head) or SEASON.search(head)) else None
                base = " | ".join([group or "", title, career or "", column or ""])
                n = seen.get(base, 0)
                seen[base] = n + 1
                rows.append(
                    Row(
                        key=base if n == 0 else f"{base} #{n + 1}",
                        ignore_key=" | ".join(x for x in (group or "", title, career or "") if x) or title,
                        title=title,
                        group=group,
                        column=column,
                        career=career,
                        raw=cell,
                        first=when.first if when else None,
                        last=when.last if when else None,
                        fixed_from=when.fixed_from if when else None,
                        acad=acad,
                        year_hint=year_hint,
                    )
                )
    return rows, re.sub(r"\s+", " ", parser.title).strip()


def looks_like_page(text: str) -> bool:
    head = text.lstrip()[:500].lower()
    return head.startswith("<") or "<html" in head or "<!doctype" in head
