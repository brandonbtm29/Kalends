"""School mode: which term a day is in, its breaks and exam weeks.

Terms come from two places: ones typed in on the School page, and ones read from
the school's academic calendar. That can be a calendar feed (.ics) or, for schools
that only publish a table on a web page, that page (see :mod:`kalends.school_page`).
Every school words things differently, so reading one is a best guess from the event
names ("Start of term", "Thanksgiving recess", "Final examinations"...). Typed-in
terms always win, and any row can be ignored from the School page.

Kalends also remembers what it read last time (``school-state.json`` beside the
config), so it can say what changed when the school updates its calendar, and keep
terms that are over once they drop off the school's page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx

from .config import AppConfig, PeriodCfg, TermCfg
from .events import Event, parse_events
from .school_page import MAX_PAGE, SEASON, Row, expected_year, looks_like_page, read_tables
from .sources import normalize_url

log = logging.getLogger("kalends.school")

PARSER = 2  # bump when the reading rules change, so changes aren't reported for them
PAGE_EVERY = timedelta(hours=6)  # web pages have no "has it changed?" header; don't fetch them often

SEASONS = {1: "Spring", 2: "Spring", 3: "Spring", 4: "Spring", 5: "Summer", 6: "Summer", 7: "Summer"}
_START = re.compile(
    r"\b(classes|instruction|semester|term|sessions?)\s+(begin|begins|start|starts|commence|commences)\b"
    r"|\bfirst\s+day\s+of\s+(classes|instruction|the\s+semester|the\s+term|semester|term)\b"
    r"|\bstart\s+of\s+(the\s+)?(term|semester|classes|instruction)\b",
    re.I,
)
_END = re.compile(
    r"\blast\s+day\s+of\s+(classes|instruction)\b|\b(classes|instruction)\s+(end|ends)\b"
    r"|\b(final|last)\s+(instructional|class)\s+day\b",
    re.I,
)
_EXAMS = re.compile(
    r"\bfinal\s+exam|\bfinals?\s+week\b|\bexam(ination)?s?\s+week\b|\bfinal\s+examinations?\b", re.I
)
_TERM_END = re.compile(
    r"\b(semester|term|sessions?)\s+(ends|end)\b|\blast\s+day\s+of\s+(the\s+)?(semester|term)\b", re.I
)
_BREAK = re.compile(
    r"\b(break|recess|vacation|holiday|thanksgiving|reading\s+day|no\s+classes|study\s+day|observance|observed)\b",
    re.I,
)
_CLOSED = re.compile(r"\b(university|campus|offices?)\s+(is\s+)?closed\b|\bclosed\b", re.I)
_TURN = re.compile(r"\b(begin|begins|resume|resumes|ends?)\b", re.I)  # "Spring break begins": a marker
_HELD = re.compile(r"\bclasses\s+(are\s+)?(held|in\s+session|meet)\b", re.I)
_HELD_FOR_SOME = re.compile(r"\b(school|college)\s+of\s+[\w .&'-]+?\s+classes\s+(are\s+)?held\b", re.I)
_SUB = re.compile(
    r"\b(1st|2nd|3rd|4th|first|second|third|fourth)\s+(\d{1,2}[- ]?(week|wk)s?|half|mini|summer\s+session)\b"
    r"|\b([1-9]|1[01])[- ]week\b|\bmini[- ]?(term|mester|session)\b",
    re.I,
)
_MAIN = re.compile(r"\b1[2-8][- ]week\b|\bregular\b|\bfull[- ]?(term|semester|session)\b", re.I)
_NAMED = re.compile(r"\b(fall|spring|summer|winter)\b(?:\s+(?:semester|term|session))?\s*(\d{4})?", re.I)
_YEAR = re.compile(r"\b(20\d\d)\b")
STRUCTURE = ("start", "end", "exams", "break", "closed")  # roles that shape the terms


def season_of(start: date) -> str:
    return SEASONS.get(start.month, "Fall" if start.month >= 8 else "Spring")


@dataclass
class Schedule:
    """Every known term, from the feed and from the School page."""

    terms: list[TermCfg] = field(default_factory=list)
    week_starts: str = "monday"

    def term_for(self, day: date) -> TermCfg | None:
        return next((t for t in self.terms if t.start <= day <= t.end), None)

    def week_of(self, term: TermCfg, day: date) -> int:
        def ws(d: date) -> date:
            off = d.weekday() if self.week_starts == "monday" else (d.weekday() + 1) % 7
            return d - timedelta(days=off)

        return (ws(day) - ws(term.start)).days // 7 + 1

    def period_for(self, day: date) -> PeriodCfg | None:
        term = self.term_for(day)
        candidates = term.periods if term else [p for t in self.terms for p in t.periods]
        return next((p for p in candidates if p.start <= day <= p.end), None)

    def between_terms(self, day: date) -> bool:
        """True on days after one known term ends and before the next begins."""
        if self.term_for(day) is not None:
            return False
        before = any(t.end < day for t in self.terms)
        after = any(t.start > day for t in self.terms)
        return before and after

    def classes_paused(self, day: date) -> bool:
        p = self.period_for(day)
        if p is not None and p.kind in ("break", "closed"):
            return True
        return self.between_terms(day)

    def label(self, day: date) -> str | None:
        """'Fall 2026 · Week 7', 'Thanksgiving Break', or 'Between terms'."""
        term = self.term_for(day)
        if term is None:
            return "Between terms" if self.between_terms(day) else None
        p = self.period_for(day)
        if p is not None and p.kind == "exams":
            return f"{term.name} · {p.name}"
        return f"{term.name} · Week {self.week_of(term, day)}"

    def day_notes(self, first: date, end_exclusive: date) -> dict[date, str]:
        """Break, exam and closed days in a range: {day: 'Thanksgiving Break'}."""
        out: dict[date, str] = {}
        d = first
        while d < end_exclusive:
            p = self.period_for(d)
            if p is not None:
                out[d] = p.name
            elif self.between_terms(d):
                out[d] = "Between terms"
            d += timedelta(days=1)
        return out


# -- what a row means ----------------------------------------------------------------------------


@dataclass
class _Flags:
    start: bool
    end: bool
    exams: bool
    off: str | None  # "break" or "closed": no classes that day
    held: bool  # a holiday on which classes are still held
    sub: bool  # about a shorter session inside the term (a 1st 8-week session...)


def _flags(title: str) -> _Flags:
    sub = bool(_SUB.search(title)) and not _MAIN.search(title)
    held = bool(_HELD.search(title)) and not _HELD_FOR_SOME.search(title)
    start = bool(_START.search(title))
    end = not start and bool(_END.search(title) or _TERM_END.search(title))
    exams = bool(_EXAMS.search(title))
    off = None
    if not exams and not held and not _TURN.search(title):
        if _CLOSED.search(title):
            off = "closed"
        elif _BREAK.search(title):
            off = "break"
    return _Flags(start, end, exams, off, held, sub)


def _name(title: str) -> str:
    """'Labor Day holiday - University closed' -> 'Labor Day holiday'; '... (no classes)' -> '...'."""
    t = re.sub(r"\s+", " ", title).strip(" -–:")
    short = re.split(r"\s+[-–—]\s+|\s*\(|;|\s+(?:beginning|starting|from)\s+", t, maxsplit=1)[0].strip(" ,")
    words = re.sub(r"\s+(observance|observed)$", "", short, flags=re.I)
    if words and words.lower() != "holiday":
        short = words
    return (short or t)[:60]


def _span(ev: Event) -> tuple[date, date]:
    """First and last date an event covers (all-day ends are exclusive)."""
    first = ev.start.date()
    last = (ev.end - timedelta(seconds=1)).date() if ev.end > ev.start else first
    return first, max(first, last)


def _prefer_main(rows: list[Row]) -> list[Row]:
    main = [r for r in rows if not _flags(r.title).sub]
    return main or rows


def _label(r: Row) -> str | None:
    """'Fall 2026' for a row in a 'Fall' group of the 2026-2027 column, or None."""
    where = r.group if SEASON.search(r.group or "") else r.column  # a "Fall 2026" column names it too
    m = SEASON.search(where or "")
    if not m or r.first is None:
        return None
    season = m.group(1).title()
    ym = _YEAR.search(where or "")
    if ym:
        year = int(ym.group(1))
    elif r.acad:
        year = expected_year(r.first.month, r.acad, None, season) or r.first.year
    else:
        year = r.year_hint or r.first.year
    return f"{season} {year}"


def build_terms(rows: list[Row], ignore: set[str] | frozenset[str] = frozenset()) -> list[TermCfg]:
    """Terms (with their breaks and exam weeks) from dated rows; also marks what each row was used for."""
    for r in rows:
        r.term = None
        r.role = "ignored" if r.ignore_key in ignore else ("" if r.first else "no date")
    usable = [r for r in rows if r.first and r.ignore_key not in ignore]
    flags = {id(r): _flags(r.title) for r in usable}
    spans: list[tuple[str, date, date]] = []

    groups: dict[str, list[Row]] = defaultdict(list)
    for r in usable:
        lab = _label(r)
        if lab:
            groups[lab].append(r)
    if groups:
        # The page says which term each row belongs to: one term per group.
        for lab, rs in groups.items():
            starts = _prefer_main([r for r in rs if flags[id(r)].start])
            ends = _prefer_main([r for r in rs if flags[id(r)].end or flags[id(r)].exams])
            if not starts or not ends:
                continue
            start = min(r.first for r in starts)
            end = max(r.last for r in ends)
            if start < end and (end - start).days <= 200:
                spans.append((lab, start, end))
    else:
        # A feed (or a plain list): one term per "classes begin", ending at the last
        # "classes end" or exam day before the next one. Shorter sessions only start a
        # term when no full term covers them (a summer made only of sessions).
        def windows(starts: list[tuple[date, Row]], ends: list[date], exams: list[Row]) -> None:
            for i, (start, row) in enumerate(starts):
                if any(s <= start <= e for _, s, e in spans):
                    continue
                nxt = starts[i + 1][0] if i + 1 < len(starts) else start + timedelta(days=150)
                window = [d for d in ends if start < d < nxt]
                window += [r.last for r in exams if start < r.first < nxt]
                if not window:
                    continue
                end = max(window)
                if (end - start).days > 160:  # not one term; probably a stray match
                    continue
                m = _NAMED.search(row.title)
                season = m.group(1).title() if m else season_of(start)
                year = m.group(2) if m and m.group(2) else str(start.year)
                spans.append((f"{season} {year}", start, end))

        main = [r for r in usable if not flags[id(r)].sub]
        for pool in (main, usable):
            starts = sorted({r.first: r for r in pool if flags[id(r)].start}.items(), key=lambda x: x[0])
            ends = [r.last for r in pool if flags[id(r)].end]
            exams = [r for r in pool if flags[id(r)].exams]
            windows(starts, ends, exams)

    terms: list[TermCfg] = []
    for name, start, end in sorted(spans, key=lambda s: s[1]):
        inside = [r for r in usable if start <= r.first and r.last <= end]
        exam_rows = _prefer_main([r for r in inside if flags[id(r)].exams])
        periods: dict[tuple, PeriodCfg] = {}  # merge duplicates (feeds often list a break twice)
        for r in inside:
            f = flags[id(r)]
            if f.exams and r in exam_rows:
                p = PeriodCfg(
                    name="Finals" if r.last > r.first else _name(r.title),
                    start=r.first,
                    end=r.last,
                    kind="exams",
                )
            elif f.off and not f.sub:
                p = PeriodCfg(name=_name(r.title), start=r.first, end=r.last, kind=f.off)
            else:
                continue
            periods.setdefault((p.start, p.end, p.kind), p)
            r.role, r.term = p.kind, name
        for r in inside:
            f = flags[id(r)]
            if r.term is None:
                r.term = name
                if f.start and r.first == start:
                    r.role = "start"
                elif (f.end or f.exams) and r.last == end:
                    r.role = "end"
                elif f.held:
                    r.role = "classes held"
                elif f.sub and (f.start or f.end or f.exams or f.off):
                    r.role = "shorter session"
        terms.append(
            TermCfg(name=name, start=start, end=end, periods=sorted(periods.values(), key=lambda p: p.start))
        )
    for r in usable:
        if r.term is None and flags[id(r)].off:
            r.role = "between terms"
    return terms


# -- reading a calendar ---------------------------------------------------------------------------


@dataclass
class Reading:
    """Everything read from the school's calendar."""

    kind: str  # "feed" (an .ics) or "page" (a web page with a table)
    rows: list[Row]
    terms: list[TermCfg]  # what Kalends uses: rows you chose to ignore are left out
    raw_terms: list[TermCfg]  # the same with nothing ignored, to notice the school's own changes
    title: str = ""

    @property
    def fixed(self) -> list[Row]:
        return [r for r in self.rows if r.fixed_from]

    @property
    def undated(self) -> list[Row]:
        return [r for r in self.rows if r.first is None]


def _feed_rows(text: str, cfg: AppConfig, today: date) -> tuple[list[Row], str]:
    tz = cfg.tz
    s = datetime.combine(today - timedelta(days=730), time.min, tzinfo=tz)
    e = datetime.combine(today + timedelta(days=730), time.min, tzinfo=tz)
    rows = []
    for ev in sorted(parse_events("_school", text, s, e, tz), key=lambda ev: ev.start):
        first, last = _span(ev)
        title = re.sub(r"\s+", " ", ev.summary).strip()
        raw = first.isoformat() if first == last else f"{first.isoformat()} – {last.isoformat()}"
        rows.append(
            Row(
                key=f"{title} | {first.isoformat()}",
                ignore_key=title,
                title=title,
                raw=raw,
                first=first,
                last=last,
            )
        )
    m = re.search(r"^X-WR-CALNAME:(.+)$", text, re.MULTILINE)
    return rows, m.group(1).strip() if m else ""


def read_calendar(text: str, cfg: AppConfig, today: date | None = None, ignore=()) -> Reading:
    """Read a feed or a web page. Raises ValueError for anything else."""
    today = today or datetime.now(cfg.tz).date()
    if "BEGIN:VCALENDAR" in text[:2000]:
        kind = "feed"
        rows, title = _feed_rows(text, cfg, today)
    elif looks_like_page(text):
        kind = "page"
        rows, title = read_tables(text)
    else:
        raise ValueError("that link didn't return a calendar feed or a web page")
    raw = build_terms(rows)
    terms = build_terms(rows, frozenset(ignore)) if ignore else raw
    return Reading(kind, rows, terms, raw, title)


def read_feed(text: str, cfg: AppConfig, today: date | None = None) -> list[TermCfg]:
    """Best-guess terms from an academic calendar feed or page (about two years either side of today)."""
    return read_calendar(text, cfg, today).terms


def build_schedule(cfg: AppConfig, feed_terms: list[TermCfg]) -> Schedule:
    manual = list(cfg.school.terms)
    merged = list(manual)
    for t in feed_terms:
        if not any(m.start <= t.end and t.start <= m.end for m in manual):
            merged.append(t)
    merged.sort(key=lambda t: t.start)
    return Schedule(terms=merged, week_starts=cfg.week_starts)


# -- remembering what it said --------------------------------------------------------------------


def fmt_span(first: date, last: date) -> str:
    """'Aug 24 – Dec 13, 2026', 'Nov 26–29, 2026', 'Dec 24, 2026 – Jan 3, 2027', 'Sep 7, 2026'."""

    def md(d: date) -> str:
        return f"{d:%b} {d.day}"

    if first == last:
        return f"{md(first)}, {first.year}"
    if first.year != last.year:
        return f"{md(first)}, {first.year} – {md(last)}, {last.year}"
    if first.month == last.month:
        return f"{md(first)}–{last.day}, {last.year}"
    return f"{md(first)} – {md(last)}, {last.year}"


def _dump_term(t: TermCfg) -> dict:
    return t.model_dump(mode="json")


def _load_term(d: dict) -> TermCfg | None:
    try:
        return TermCfg.model_validate(d)
    except Exception:  # noqa: BLE001 - an old or hand-edited entry is just skipped
        return None


def describe_changes(old_terms: list[TermCfg], new_terms: list[TermCfg], old_rows: dict, new_rows: dict,
                     rows: list[Row], today: date) -> list[str]:  # fmt: skip
    """Plain-language lines about what changed between two readings."""
    lines: list[str] = []
    ob = {t.name: t for t in old_terms}
    nb = {t.name: t for t in new_terms}
    for t in sorted((nb[n] for n in nb.keys() - ob.keys()), key=lambda t: t.start):
        lines.append(f"{t.name} added ({fmt_span(t.start, t.end)})")
    for t in sorted((ob[n] for n in ob.keys() - nb.keys()), key=lambda t: t.start):
        over = " (it's over, so Kalends keeps it)" if t.end < today else ""
        lines.append(f"{t.name} is no longer listed{over}")
    for name in sorted(ob.keys() & nb.keys(), key=lambda n: nb[n].start):
        o, n = ob[name], nb[name]
        if (o.start, o.end) != (n.start, n.end):
            lines.append(f"{name} now runs {fmt_span(n.start, n.end)} (was {fmt_span(o.start, o.end)})")
        op = {(p.kind, p.name): p for p in o.periods}
        np_ = {(p.kind, p.name): p for p in n.periods}
        for k in sorted(np_.keys() - op.keys(), key=lambda k: np_[k].start):
            p = np_[k]
            lines.append(f"{name}: {p.name} added ({fmt_span(p.start, p.end)})")
        for k in sorted(op.keys() - np_.keys(), key=lambda k: op[k].start):
            lines.append(f"{name}: {op[k].name} removed")
        for k in sorted(op.keys() & np_.keys(), key=lambda k: np_[k].start):
            a, b = op[k], np_[k]
            if (a.start, a.end) != (b.start, b.end):
                lines.append(
                    f"{name}: {b.name} now {fmt_span(b.start, b.end)} (was {fmt_span(a.start, a.end)})"
                )
    by_key = {r.key: r for r in rows}
    moved = [
        k
        for k in sorted(new_rows.keys() & old_rows.keys())
        if new_rows[k] != old_rows[k] and by_key.get(k) is not None and by_key[k].role not in STRUCTURE
    ]
    for k in moved[:4]:
        r, was = by_key[k], old_rows[k]
        where = f" ({r.column})" if r.column else ""
        now = fmt_span(r.first, r.last) if r.first else "no date"
        before = fmt_span(date.fromisoformat(was[0]), date.fromisoformat(was[1])) if was else "no date"
        lines.append(f"{r.title}{where}: now {now} (was {before})")
    if len(moved) > 4:
        lines.append(f"{len(moved) - 4} more dates moved")
    if not lines:
        added, gone = len(new_rows.keys() - old_rows.keys()), len(old_rows.keys() - new_rows.keys())
        if added or gone:
            parts = [f"{added} date{'s' if added != 1 else ''} added"] if added else []
            parts += [f"{gone} removed"] if gone else []
            lines.append(", ".join(parts).capitalize())
    return lines


class SchoolMemory:
    """What the school's calendar said last time, and the changes Kalends noticed (school-state.json)."""

    FILE = "school-state.json"

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                log.warning("couldn't read %s; starting fresh", path.name)

    def _save(self) -> None:
        if self.path is None:
            return
        text = json.dumps(self.data, indent=1, sort_keys=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".school-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("couldn't save %s: %s", self.path.name, exc)

    def observe(self, source: str, reading: Reading, today: date) -> list[str]:
        """Compare with last time; returns what changed (nothing the first time a calendar is read)."""
        rows = {r.key: [r.first.isoformat(), r.last.isoformat()] if r.first else None for r in reading.rows}
        terms = [_dump_term(t) for t in reading.raw_terms]
        with self._lock:
            d = self.data
            if d.get("source") != source:
                self.data = {
                    "source": source,
                    "parser": PARSER,
                    "rows": rows,
                    "terms": terms,
                    "kept": [],
                    "changes": [],
                }
                self._save()
                return []
            if d.get("parser") != PARSER or "rows" not in d:
                d.update(parser=PARSER, rows=rows, terms=terms)  # read differently now: a new baseline
                self._save()
                return []
            if rows == d.get("rows") and terms == d.get("terms"):
                return []
            old_terms = [t for t in (_load_term(x) for x in d.get("terms") or []) if t]
            lines = describe_changes(
                old_terms, reading.raw_terms, d.get("rows") or {}, rows, reading.rows, today
            )
            d.update(rows=rows, terms=terms)
            if lines:
                self._add(lines)
            self._save()
            return lines

    def note(self, lines: list[str]) -> None:
        with self._lock:
            self._add(lines)
            self._save()

    def _add(self, lines: list[str]) -> None:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self.data["changes"] = ([{"at": stamp, "lines": lines}] + list(self.data.get("changes") or []))[:5]
        self.data["unseen"] = True

    def remember(self, source: str, terms: list[TermCfg]) -> None:
        """Keep the terms in use, so ones that are over survive the school dropping them."""
        with self._lock:
            if self.data.get("source") != source:
                return
            kept = {t["name"]: t for t in self.data.get("kept") or [] if isinstance(t, dict) and "name" in t}
            for t in terms:
                kept[t.name] = _dump_term(t)
            newest = sorted(kept.values(), key=lambda t: str(t.get("start")), reverse=True)[:12]
            if newest != sorted(self.data.get("kept") or [], key=lambda t: str(t.get("start")), reverse=True):
                self.data["kept"] = newest
                self._save()

    def past_terms(self, source: str, today: date) -> list[TermCfg]:
        with self._lock:
            if self.data.get("source") != source:
                return []
            kept = [t for t in (_load_term(x) for x in self.data.get("kept") or []) if t]
        return [t for t in kept if t.end < today]

    def latest(self) -> dict | None:
        """The newest change the owner hasn't dismissed yet."""
        with self._lock:
            changes = self.data.get("changes") or []
            if not (self.data.get("unseen") and changes):
                return None
            c = dict(changes[0])
        try:
            c["when"] = datetime.fromisoformat(c["at"])
        except (KeyError, ValueError):
            c["when"] = None
        return c

    def dismiss(self) -> None:
        with self._lock:
            if self.data.get("unseen"):
                self.data["unseen"] = False
                self._save()


def source_id(url: str | None) -> str:
    return hashlib.sha256(normalize_url(url or "").encode()).hexdigest()[:16]


class SchoolCalendar:
    """Downloads the academic calendar (if any) and answers term questions."""

    def __init__(self, cfg: AppConfig, memory: SchoolMemory | None = None, today=None):
        self.cfg = cfg
        self.today = today or (lambda: datetime.now(cfg.tz).date())
        self.memory = memory or SchoolMemory(None)
        self.text: str | None = None
        self.etag: str | None = None
        self.modified: str | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.reading: Reading | None = None
        self.feed_terms: list[TermCfg] = []
        self.new_changes: list[str] = []  # noticed on the last refresh; the runtime sends them on
        self._schedule: Schedule | None = None
        self.version = 0  # bumps when the calendar's terms change
        self._load_disk()

    @property
    def kind(self) -> str | None:
        return self.reading.kind if self.reading else None

    def carry_over(self, old: SchoolCalendar) -> None:
        """Keep what an earlier copy downloaded (same link, new settings)."""
        if old.text and self.cfg.school.calendar_url == old.cfg.school.calendar_url:
            self.etag, self.modified = old.etag, old.modified
            self.last_success, self.last_error = old.last_success, old.last_error
            self._set_text(old.text)

    # -- download ---------------------------------------------------------------------------

    def _disk_path(self) -> Path:
        h = hashlib.sha256(b"_school").hexdigest()[:16]
        return Path(self.cfg.cache_dir) / f"{h}.ics.cache"

    def _load_disk(self) -> None:
        url = self.cfg.school.calendar_url
        p = self._disk_path()
        if url and p.is_file():
            try:
                stored = p.read_text(encoding="utf-8")
                tag, _, body = stored.partition("\n")
                if tag == hashlib.sha256(url.encode()).hexdigest():
                    self._set_text(body)
            except OSError:
                pass

    def _set_text(self, text: str, reading: Reading | None = None) -> None:
        self.text = text
        if reading is None:
            try:
                reading = read_calendar(text, self.cfg, self.today(), ignore=self.cfg.school.ignore)
            except Exception as exc:  # a strange calendar should never break the pages
                log.warning("couldn't read the academic calendar: %s", exc)
        self.reading = reading
        self.feed_terms = reading.terms if reading else []
        url = self.cfg.school.calendar_url
        if reading is not None and url:
            src = source_id(url)
            lines = self.memory.observe(src, reading, self.today())
            if lines:
                self.new_changes = lines
            self.memory.remember(src, reading.terms)
        self._schedule = None
        self.version += 1

    def refresh(self, client: httpx.Client, force: bool = False) -> None:
        sc = self.cfg.school
        if not (sc.enabled and sc.calendar_url):
            return
        if (
            not force
            and self.kind == "page"
            and self.last_success is not None
            and datetime.now(UTC) - self.last_success < PAGE_EVERY
        ):
            return
        url = normalize_url(sc.calendar_url)
        try:
            if url.startswith("file://") or "://" not in url:
                text = Path(url.removeprefix("file://")).read_text(encoding="utf-8")
            else:
                headers = {}
                if self.etag:
                    headers["If-None-Match"] = self.etag
                if self.modified:
                    headers["If-Modified-Since"] = self.modified
                resp = client.get(url, headers=headers)
                if resp.status_code == 304:
                    self.last_success, self.last_error = datetime.now(UTC), None
                    return
                resp.raise_for_status()
                if resp.content[:5] == b"%PDF-":
                    raise ValueError(
                        "that link is a PDF. Use the web page with the table, or a calendar feed (.ics)"
                    )
                text = resp.text[:MAX_PAGE]
                if "BEGIN:VCALENDAR" not in text[:2000] and not looks_like_page(text):
                    raise ValueError("that link didn't return a calendar feed or a web page")
                self.etag = resp.headers.get("ETag")
                self.modified = resp.headers.get("Last-Modified")
            if text != self.text:
                reading = read_calendar(text, self.cfg, self.today(), ignore=sc.ignore)
                if reading.kind == "page":
                    if self.reading is not None and self.reading.terms and not reading.terms:
                        msg = (
                            "The school's page didn't have its table of dates this time, "
                            "so Kalends is still using what it read before."
                        )
                        if self.last_error != msg:
                            self.new_changes = [msg]
                            self.memory.note([msg])
                        raise _Kept(msg)
                    if not any(r.first for r in reading.rows):
                        raise ValueError(
                            "Kalends couldn't find a table of dates on that page. If the page builds "
                            "its table with JavaScript, Kalends can't read it; type the terms in instead"
                        )
                self._set_text(text, reading)
                try:
                    self._disk_path().parent.mkdir(parents=True, exist_ok=True)
                    tag = hashlib.sha256(sc.calendar_url.encode()).hexdigest()
                    self._disk_path().write_text(tag + "\n" + text, encoding="utf-8")
                except OSError as exc:
                    log.warning("couldn't cache the academic calendar: %s", exc)
            self.last_success, self.last_error = datetime.now(UTC), None
        except _Kept as exc:
            self.last_error = str(exc)
            log.warning("academic calendar: %s", self.last_error)
        except Exception as exc:
            detail = re.sub(r"\w+://\S+", "<url>", str(exc))
            self.last_error = (
                detail[:1].upper() + detail[1:]
                if isinstance(exc, ValueError)
                else (f"{type(exc).__name__}: {detail}")
            )
            log.warning("academic calendar: %s", self.last_error)

    # -- questions ----------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.cfg.school.enabled

    @property
    def schedule(self) -> Schedule:
        if self._schedule is None:
            url = self.cfg.school.calendar_url
            terms = list(self.feed_terms) if url else []
            if url:
                # Terms that are over and have dropped off the school's page
                for t in self.memory.past_terms(source_id(url), self.today()):
                    if not any(o.name == t.name or (o.start <= t.end and t.start <= o.end) for o in terms):
                        terms.append(t)
            self._schedule = build_schedule(self.cfg, terms)
        return self._schedule

    def filter(self, events: list[Event]) -> list[Event]:
        """Drop class events on break days and between terms (when school mode says so)."""
        sc = self.cfg.school
        if not (sc.enabled and sc.pause_classes and sc.class_calendars):
            return events
        sched = self.schedule
        if not sched.terms:
            return events
        classes = set(sc.class_calendars)
        return [
            ev for ev in events if ev.calendar_id not in classes or not sched.classes_paused(ev.start.date())
        ]

    def viewer_context(self, first: date, end_exclusive: date, anchor: date) -> dict:
        """What viewers see: a term label for the period and notes on break/exam days."""
        sc = self.cfg.school
        if not (sc.enabled and sc.show_viewers):
            return {"term_label": None, "school_days": {}}
        sched = self.schedule
        return {"term_label": sched.label(anchor), "school_days": sched.day_notes(first, end_exclusive)}


class _Kept(Exception):
    """A download that would have thrown away a good reading; the old one stays."""


def check_link(url: str, client: httpx.Client, cfg: AppConfig) -> dict:
    """Fetch a school calendar link once and say what Kalends would read from it ('Check link')."""
    url = normalize_url(url.strip())
    if not url:
        return {"ok": False, "error": "Paste a link first."}
    if not re.match(r"^https?://", url):
        return {"ok": False, "error": "Links should start with https:// or webcal://"}
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Couldn't reach it ({type(exc).__name__})."}
    if resp.status_code != 200:
        return {"ok": False, "error": f"The server answered HTTP {resp.status_code}. Check the link."}
    if resp.content[:5] == b"%PDF-":
        return {
            "ok": False,
            "error": "That link is a PDF. Use the web page with the table, or a calendar feed (.ics).",
        }
    try:
        reading = read_calendar(resp.text[:MAX_PAGE], cfg, ignore=cfg.school.ignore)
    except ValueError:
        return {"ok": False, "error": "That link didn't return a calendar feed (ICS) or a web page."}
    except Exception as exc:  # noqa: BLE001 - show any problem on the page
        return {"ok": False, "error": f"Kalends couldn't read it ({type(exc).__name__})."}
    if reading.kind == "page" and not any(r.first for r in reading.rows):
        return {
            "ok": False,
            "error": "That page doesn't have a table of dates Kalends can read. If the page builds its "
            "table with JavaScript, Kalends can't see it; type the terms in instead.",
        }
    return {
        "ok": True,
        "kind": reading.kind,
        "name": reading.title,
        "terms": reading.terms,
        "rows": len(reading.rows),
        "fixed": reading.fixed,
        "undated": len(reading.undated),
    }
