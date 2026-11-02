# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.14.0: school calendars published as a web page table, checked for changes, and reviewable."""

from __future__ import annotations

import html
import json
import re
import types
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from test_v050 import csrf_rev, panel, store  # noqa: F401

from kalends import school
from kalends.school import Schedule, SchoolCalendar, SchoolMemory, check_link, read_calendar
from kalends.school_page import parse_when, read_tables

URL = "https://registrar.example.edu/dates/acadcal"

# A table laid out like the University of Akron's registrar page: Term | Event | Career | two years.
# It includes their real-world quirks: shorter sessions with their own "final examinations", a
# holiday on which classes are held, rows with no date yet, and two years typed one off.
FALL = [
    ("Fall", "Start of term", "G/U", "Aug 24, 2026", "Aug 23, 2027"),
    ("Fall", "15 week, 1st 8 week, and 1st 5 week sessions begin", "G/U", "Aug 24, 2026", "Aug 23, 2027"),
    ("Fall", 'Last day to drop 15 week session classes without "WD"', "G/U", "Sep 6, 2026", "Sep 5, 2027"),
    ("Fall", "Labor Day holiday - University closed", "G/U", "Sep 7, 2026", "Sep 6, 2027"),
    ("Fall", "1st 8 week session ends", "G/U", "Oct 11, 2026", "Oct 17, 2027"),
    ("Fall", "1st 8 week final examinations", "G/U", "Oct 12-18, 2026", "Oct 18-24, 2027"),
    ("Fall", "2nd 8 week session begins", "G/U", "Oct 19, 2026", "Oct 18, 2027"),
    ("Fall", "Graduation application deadline", "U", "Oct 1, 2026", "Oct 1, 2027"),
    ("Spring", "Registration begins", "G/U", "Mid-March", "Mid-March"),
    ("Fall", "Graduation application deadline", "G/U", "Nov 1, 2026", "Nov 1, 2027"),
    ("Fall", "Veteran's Day observance - Staff holiday, classes held", "G/U", "Nov 11, 2026", "Nov 11, 2027"),
    (
        "Fall",
        "Thanksgiving recess beginning at 4:00pm on Wednesday",
        "G/U",
        "Nov 26-29, 2026",
        "Nov 25-28, 2027",
    ),
    ("Fall", "Final instructional day", "G/U", "Dec 6, 2026", "Dec 5, 2027"),
    ("Fall", "15 week, 2nd 8 week, and 3rd 5 week sessions end", "G/U", "Dec 6, 2026", "Dec 5, 2027"),
    ("Fall", "Final examinations", "G/U", "Dec 7-13, 2026", "Dec 6-12, 2027"),
    ("Fall", "Commencement", "G/U", "Dec 11-12, 2026", "Dec 10-11, 2027"),
    ("Fall", "Final grades due", "G/U", "Dec 15, 2026", "Dec 14, 2027"),
    ("", "Holiday observance - University closed", "G/U", "Dec 24-31, 2026", "Dec 23-31, 2027"),
    ("", "Audit 60+ in-person registration", "G/U", "TBD", "TBD"),
    ("", "New Year's Day Observed", "G/U", "Jan 1, 2027", "Jan 1, 2028"),
]
SPRING = [
    ("Spring", "Start of term", "G/U", "Jan 11, 2027", "Jan 10, 2028"),
    (
        "Spring",
        "Martin Luther King Jr. Day observance - University closed",
        "G/U",
        "Jan 18, 2027",
        "Jan 17, 2028",
    ),
    (
        "Spring",
        "President's Day observance (School of Law classes held)",
        "G/U",
        "Feb 16, 2027",
        "Feb 22, 2028",
    ),
    ("Spring", "1st 8 week final examinations", "G/U", "Mar 1-Mar 7, 2027", "Mar 6-12, 2028"),
    ("Spring", "Spring recess", "G/U", "Mar 22-28, 2027", "Mar 20-26, 2028"),
    ("Spring", "Final instructional day", "G/U", "May 2, 2027", "Apr 30, 2028"),
    ("Spring", "15 week, 2nd 8 week, and 3rd 5 week sessions end", "G/U", "May 2, 2027", "Apr 30, 2028"),
    ("Spring", "Final examinations", "G/U", "May 3-9, 2027", "May 1-7, 2028"),
    ("Spring", "Final grades due", "G/U", "May 11, 2027", "May 9, 2028"),
]
SUMMER = [
    ("Summer", "Intersession, Regular, and 1st 7 week sessions begin", "G/U", "May 17, 2027", "May 15, 2028"),
    ("Summer", "Last day to withdraw from intersession classes", "G/U", "May 25, 2027", "May 25, 2027"),
    (
        "Summer",
        'Last day to drop Regular session classes without "WD"',
        "G/U",
        "May 31, 2026",
        "May 29, 2028",
    ),
    ("Summer", "Memorial Day observance - University closed", "G/U", "May 31, 2027", "May 29, 2028"),
    ("Summer", "1st 5 week and 10 week sessions begin", "G/U", "Jun 1, 2027", "May 30, 2028"),
    ("Summer", "Juneteenth observance - University closed", "G/U", "Jun 18, 2027", "Jun 19, 2028"),
    ("Summer", "Independence Day observance - University closed", "G/U", "Jul 5, 2027", "Jul 4, 2028"),
    ("Summer", "2nd 5 week and 10 week sessions end", "G/U", "Aug 8, 2027", "Aug 6, 2028"),
    ("Summer", "Regular and 2nd 7 week sessions end", "G/U", "Aug 22, 2027", "Aug 20, 2028"),
    ("Summer", "Final grades due", "G/U", "Aug 24, 2027", "Aug 22, 2028"),
]
ROWS = FALL + SPRING + SUMMER


def page(rows=ROWS, years=("2026-2027", "2027-2028"), title="Academic Calendar | Example University"):
    def td(text):
        return f'<td style="width: 90px;">{html.escape(text)}</td>'

    body = "".join(
        "<tr class='success'>" + td(t) + f"<td><strong>{html.escape(e)}</strong></td>" + td(c)
        + "".join(td(x) for x in cols) + "</tr>\n"
        for t, e, c, *cols in rows
    )  # fmt: skip
    heads = "".join(f"<th>{y}</th>" for y in years)
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title><script>var x = '<table>';</script></head>"
        "<body><nav><ul><li>Home</li></ul></nav><h1>Academic Calendar</h1>"
        "<p>NOTE: in the column labeled <strong>Career</strong>, G is graduate, U is undergraduate.</p>"
        '<div class="table-responsive"><table class="table table-hover" id="myTable">'
        f"<thead class='thead-dark'><tr><th scope='col'>Term</th><th scope='col'>Event</th>"
        f"<th scope='col'>Career</th>{heads}</tr></thead><tbody>\n{body}</tbody></table></div></body></html>"
    )


def by_name(terms):
    return {t.name: t for t in terms}


def periods(term):
    return {(p.kind, p.name, p.start, p.end) for p in term.periods}


@pytest.fixture
def cfg(store):
    return store.current()


# -- reading a table -----------------------------------------------------------------------------


def test_a_registrar_table_becomes_terms(cfg):
    r = read_calendar(page(), cfg, today=date(2026, 11, 2))
    assert r.kind == "page" and r.title == "Academic Calendar | Example University"
    assert [t.name for t in r.terms] == [
        "Fall 2026", "Spring 2027", "Summer 2027", "Fall 2027", "Spring 2028", "Summer 2028",
    ]  # fmt: skip
    t = by_name(r.terms)
    fall = t["Fall 2026"]
    assert (fall.start, fall.end) == (date(2026, 8, 24), date(2026, 12, 13))  # through finals week
    assert periods(fall) == {
        ("closed", "Labor Day holiday", date(2026, 9, 7), date(2026, 9, 7)),
        ("break", "Thanksgiving recess", date(2026, 11, 26), date(2026, 11, 29)),
        ("exams", "Finals", date(2026, 12, 7), date(2026, 12, 13)),
    }  # not the 1st 8-week session's exams, and not Veterans Day (classes are held)
    spring = t["Spring 2027"]
    assert (spring.start, spring.end) == (date(2027, 1, 11), date(2027, 5, 9))
    assert {p.name for p in spring.periods} == {
        "Martin Luther King Jr. Day",
        "President's Day",
        "Spring recess",
        "Finals",
    }
    summer = t["Summer 2027"]
    assert (summer.start, summer.end) == (date(2027, 5, 17), date(2027, 8, 22))  # the Regular session
    assert {p.name for p in summer.periods} == {"Memorial Day", "Juneteenth", "Independence Day"}
    assert by_name(r.terms)["Fall 2027"].start == date(2027, 8, 23)

    roles = {(x.title, x.column): x.role for x in r.rows}
    assert roles[("Veteran's Day observance - Staff holiday, classes held", "2026-2027")] == "classes held"
    assert roles[("1st 8 week final examinations", "2026-2027")] == "shorter session"
    assert roles[("Start of term", "2026-2027")] == "start"
    assert roles[("Holiday observance - University closed", "2026-2027")] == "between terms"
    assert roles[("Audit 60+ in-person registration", "2026-2027")] == "no date"
    assert roles[('Last day to drop 15 week session classes without "WD"', "2026-2027")] == ""
    assert len(r.undated) == 4  # TBD and Mid-March, in both years

    fixed = {(x.title, x.column): (x.fixed_from, x.first) for x in r.fixed}
    assert fixed == {
        ("Last day to withdraw from intersession classes", "2027-2028"): ("May 25, 2027", date(2028, 5, 25)),
        ('Last day to drop Regular session classes without "WD"', "2026-2027"): (
            "May 31, 2026",
            date(2027, 5, 31),
        ),
    }  # fmt: skip


def test_weeks_breaks_and_finals_from_the_table(cfg):
    sched = Schedule(terms=read_calendar(page(), cfg, today=date(2026, 11, 2)).terms)
    assert sched.label(date(2026, 10, 14)) == "Fall 2026 · Week 8"  # not "Finals": that's an 8-week session
    assert sched.label(date(2026, 12, 8)) == "Fall 2026 · Finals"
    assert sched.classes_paused(date(2026, 11, 27)) and sched.classes_paused(date(2026, 9, 7))
    assert not sched.classes_paused(date(2026, 11, 11))  # Veterans Day: classes held
    assert sched.classes_paused(date(2026, 12, 28)) and sched.label(date(2026, 12, 28)) == "Between terms"
    assert sched.classes_paused(date(2027, 2, 16))  # Presidents' Day (only the law school meets)
    assert sched.label(date(2027, 6, 1)) == "Summer 2027 · Week 3"


@pytest.mark.parametrize(
    ("text", "first", "last"),
    [
        ("Aug 24, 2026", date(2026, 8, 24), date(2026, 8, 24)),
        ("Oct 12-18, 2026", date(2026, 10, 12), date(2026, 10, 18)),
        ("Mar 1-Mar 7, 2027", date(2027, 3, 1), date(2027, 3, 7)),
        ("Nov 26 – Dec 1, 2026", date(2026, 11, 26), date(2026, 12, 1)),
        ("Dec 28, 2026 - Jan 3, 2027", date(2026, 12, 28), date(2027, 1, 3)),
        ("Dec 28 - Jan 3, 2027", date(2026, 12, 28), date(2027, 1, 3)),
        ("Thursday, November 26 through Sunday, November 29, 2026", date(2026, 11, 26), date(2026, 11, 29)),
        ("Mon., Aug. 24", date(2026, 8, 24), date(2026, 8, 24)),
        ("August 24th", date(2026, 8, 24), date(2026, 8, 24)),
        ("8/24/2026", date(2026, 8, 24), date(2026, 8, 24)),
        ("8/24/26 - 8/28/26", date(2026, 8, 24), date(2026, 8, 28)),
        ("2026-08-24", date(2026, 8, 24), date(2026, 8, 24)),
        ("24 August 2026", date(2026, 8, 24), date(2026, 8, 24)),
        ("Dec 7, 8:00 a.m.", date(2026, 12, 7), date(2026, 12, 7)),
    ],
)
def test_dates_in_many_formats(text, first, last):
    when = parse_when(text, lambda m: 2026)
    assert (when.first, when.last) == (first, last) and when.fixed_from is None


@pytest.mark.parametrize("text", ["TBD", "Mid-March", "", "Fall"])
def test_cells_without_a_day(text):
    assert parse_when(text, lambda m: 2026) is None


def test_a_year_one_off_is_corrected_only_when_the_column_says_which_year(cfg):
    # In an academic-year column, Fall dates belong to the first year and Spring ones to the second.
    fix = [("Spring", "Start of term", "Jan 11, 2026"), ("Spring", "Final examinations", "May 3-9, 2027")]
    rows, _ = read_tables(page([(t, e, "", d) for t, e, d in fix], years=("2026-2027",)))
    assert [(r.first, r.fixed_from) for r in rows] == [
        (date(2027, 1, 11), "Jan 11, 2026"),
        (date(2027, 5, 3), None),
    ]
    # Two years off isn't a typo Kalends can be sure of; and plain "Date" columns are left alone.
    rows, _ = read_tables(page([("Spring", "Start of term", "", "Jan 11, 2029")], years=("2026-2027",)))
    assert rows[0].first == date(2029, 1, 11) and rows[0].fixed_from is None
    plain = (
        "<table><tr><th>Date</th><th>Event</th></tr>"
        "<tr><td>Jan 11, 2026</td><td>Classes begin</td></tr></table>"
    )
    assert read_tables(plain)[0][0].fixed_from is None


def test_a_page_with_a_heading_and_table_per_term(cfg):
    text = """<html><body>
    <h2>Fall Semester 2026</h2>
    <table><tr><th>Date</th><th>Event</th></tr>
      <tr><td>Monday, August 24</td><td>Classes begin</td></tr>
      <tr><td>September 7</td><td>Labor Day (no classes)</td></tr>
      <tr><td>November 25 &ndash; 29</td><td>Thanksgiving Break</td></tr>
      <tr><td>Friday, December 11</td><td>Last day of classes</td></tr>
      <tr><td>December 14 &ndash; 18</td><td>Final exams</td></tr>
    </table>
    <h2>Spring Semester 2027</h2>
    <table><tr><th>Date</th><th>Event</th></tr>
      <tr><td>Jan 19</td><td>First day of classes</td></tr>
      <tr><td>Mar 15-19</td><td>Spring Break</td></tr>
      <tr><td>May 7</td><td>Last day of classes</td></tr>
    </table></body></html>"""
    t = by_name(read_calendar(text, cfg, today=date(2026, 11, 2)).terms)
    assert set(t) == {"Fall 2026", "Spring 2027"}
    assert (t["Fall 2026"].start, t["Fall 2026"].end) == (date(2026, 8, 24), date(2026, 12, 18))
    assert {p.name for p in t["Fall 2026"].periods} == {"Labor Day", "Thanksgiving Break", "Finals"}
    assert (t["Spring 2027"].start, t["Spring 2027"].end) == (date(2027, 1, 19), date(2027, 5, 7))


def test_columns_named_after_terms_and_spanning_rows(cfg):
    by_column = """<table>
      <tr><th>Event</th><th>Fall 2026</th><th>Spring 2027</th></tr>
      <tr><td>Classes begin</td><td>Aug 26</td><td>Jan 20</td></tr>
      <tr><td>Break (no classes)</td><td>Oct 12-13</td><td>Mar 16-20</td></tr>
      <tr><td>Last day of classes</td><td>Dec 9</td><td>May 5</td></tr>
      <tr><td>Final exams</td><td>Dec 11-17</td><td>May 7-13</td></tr>
    </table>"""
    t = by_name(read_calendar(by_column, cfg, today=date(2026, 11, 2)).terms)
    assert (t["Fall 2026"].start, t["Fall 2026"].end) == (date(2026, 8, 26), date(2026, 12, 17))
    assert (t["Spring 2027"].start, t["Spring 2027"].end) == (date(2027, 1, 20), date(2027, 5, 13))
    sections = """<table>
      <tr><th>Date</th><th>What</th></tr>
      <tr><td colspan="2"><b>Fall 2026</b></td></tr>
      <tr><td>8/24/2026</td><td>Classes begin</td></tr>
      <tr><td>12/11/2026</td><td>Classes end</td></tr>
      <tr><td colspan="2">Spring 2027</td></tr>
      <tr><td>1/19/2027</td><td>Classes begin</td></tr>
      <tr><td>5/7/2027</td><td>Classes end</td></tr>
    </table>"""
    assert [t.name for t in read_calendar(sections, cfg, today=date(2026, 11, 2)).terms] == [
        "Fall 2026",
        "Spring 2027",
    ]
    spans = """<table><thead><tr><th>Term</th><th>Event</th><th>Date</th></tr></thead>
      <tr><td rowspan="3">Fall 2026</td><td>First day of classes</td><td>8/24/2026</td></tr>
      <tr><td>Thanksgiving recess</td><td>11/25/2026 - 11/29/2026</td></tr>
      <tr><td>Final exams week</td><td>12/7/2026 - 12/12/2026</td></tr>
    </table>"""
    (fall,) = read_calendar(spans, cfg, today=date(2026, 11, 2)).terms
    assert fall.name == "Fall 2026" and fall.end == date(2026, 12, 12)
    assert ("break", "Thanksgiving recess", date(2026, 11, 25), date(2026, 11, 29)) in periods(fall)


def test_feeds_ignore_shorter_sessions_too(cfg):
    ics = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0"]
        + [
            line
            for i, (d1, d2, title) in enumerate(
                [
                    ("20260824", "20260825", "Fall classes begin"),
                    ("20261019", "20261020", "Second 8-week session begins"),
                    ("20261012", "20261017", "First 8-week session final exams"),
                    ("20261207", "20261213", "Final exams"),
                ]
            )
            for line in (
                "BEGIN:VEVENT",
                f"UID:{i}",
                f"DTSTART;VALUE=DATE:{d1}",
                f"DTEND;VALUE=DATE:{d2}",
                f"SUMMARY:{title}",
                "END:VEVENT",
            )  # fmt: skip
        ]
        + ["END:VCALENDAR", ""]
    )
    (fall,) = read_calendar(ics, cfg, today=date(2026, 11, 2)).terms
    assert fall.name == "Fall 2026" and fall.end == date(2026, 12, 12)
    assert [p.start for p in fall.periods if p.kind == "exams"] == [date(2026, 12, 7)]


def test_ignoring_a_row_leaves_it_out_every_year(cfg):
    key = "Spring | President's Day observance (School of Law classes held) | G/U"
    r = read_calendar(page(), cfg, today=date(2026, 11, 2), ignore=[key])
    for name in ("Spring 2027", "Spring 2028"):
        assert "President's Day" not in {p.name for p in by_name(r.terms)[name].periods}
        assert "President's Day" in {p.name for p in by_name(r.raw_terms)[name].periods}
    assert {x.role for x in r.rows if x.ignore_key == key} == {"ignored"}


# -- downloading and keeping up --------------------------------------------------------------------


class Site:
    """A school website: serves whatever page is current and counts visits."""

    def __init__(self, text: str, content_type: str = "text/html; charset=UTF-8"):
        self.text, self.type, self.hits = text, content_type, 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.hits += 1
        body = self.text.encode() if isinstance(self.text, str) else self.text
        return httpx.Response(200, content=body, headers={"Content-Type": self.type})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def school_on(d, url=URL, **extra):
    node = d.section("school")
    node.update({"enabled": True, "name": "Example University", "calendar_url": url, **extra})


def test_pages_are_read_kept_and_checked_every_few_hours(store, tmp_path):
    store.update(school_on)
    site = Site(page())
    sc = SchoolCalendar(store.current(), SchoolMemory(tmp_path / "school-state.json"))
    sc.refresh(site.client())
    assert sc.last_error is None and sc.kind == "page" and len(sc.feed_terms) == 6
    sc.refresh(site.client())  # a page has no "changed?" header: not again for a few hours
    assert site.hits == 1
    sc.refresh(site.client(), force=True)
    assert site.hits == 2
    sc.last_success = datetime.now(UTC) - school.PAGE_EVERY - timedelta(minutes=1)
    sc.refresh(site.client())
    assert site.hits == 3
    # after a restart the copy on disk is used straight away
    again = SchoolCalendar(store.current(), SchoolMemory(tmp_path / "school-state.json"))
    assert [t.name for t in again.feed_terms] == [t.name for t in sc.feed_terms]

    # a broken page (maintenance, a redesign) doesn't throw away good terms
    site.text = "<html><body><h1>Down for maintenance</h1></body></html>"
    sc.refresh(site.client(), force=True)
    assert len(sc.feed_terms) == 6 and "still using what it read before" in sc.last_error
    assert sc.new_changes and "still using" in sc.new_changes[0]


def test_links_that_are_not_calendars(store):
    store.update(school_on)
    cfg = store.current()
    pdf = Site(b"%PDF-1.7 ...", "application/pdf")
    sc = SchoolCalendar(cfg)
    sc.refresh(pdf.client())
    assert sc.last_error.startswith("That link is a PDF") and not sc.feed_terms
    js = Site("<html><body><div id='app'></div><script src='calendar.js'></script></body></html>")
    sc.refresh(js.client(), force=True)
    assert "couldn't find a table of dates" in sc.last_error
    assert check_link(URL, pdf.client(), cfg)["error"].startswith("That link is a PDF")
    assert "JavaScript" in check_link(URL, js.client(), cfg)["error"]
    ok = check_link(URL, Site(page()).client(), cfg)
    assert ok["ok"] and ok["kind"] == "page" and len(ok["terms"]) == 6 and len(ok["fixed"]) == 2
    assert ok["undated"] == 4 and ok["name"].startswith("Academic Calendar")


def moved(rows, title, *dates):
    return [(t, e, c, *dates) if e == title else (t, e, c, *cols) for t, e, c, *cols in rows]


def test_changes_are_noticed_shown_and_sent(panel, monkeypatch):
    rt, c = panel
    rt.apply(rt.store.update(school_on), fetch=False)
    sent = []
    monkeypatch.setattr(rt.notifier, "owner", lambda note, wait=False: sent.append(note))
    site = Site(page())
    rt.check_school(site.client(), force=True)
    assert sent == [] and rt.kalends.school.memory.latest() is None  # the first read is the baseline
    state = rt.store.config_path.parent / SchoolMemory.FILE
    assert json.loads(state.read_text(encoding="utf-8"))["terms"]

    site.text = page(moved(ROWS, "Spring recess", "Mar 15-21, 2027", "Mar 20-26, 2028"))
    rt.check_school(site.client(), force=True)
    assert len(sent) == 1
    note = sent[0]
    assert note.title == "Example University: academic calendar changed" and note.url.endswith("/school")
    assert "Spring 2027: Spring recess now Mar 15–21, 2027 (was Mar 22–28, 2027)" in note.body
    sched = rt.kalends.school.schedule
    assert sched.period_for(date(2027, 3, 16)).name == "Spring recess"

    html_ = c.get("/school").text
    assert "Example University changed its academic calendar" in html_ and "Spring recess now" in html_
    assert "changed its academic calendar" in c.get("/").text
    # nothing new: no second message
    rt.check_school(site.client(), force=True)
    assert len(sent) == 1
    csrf, _ = csrf_rev(c, "/school")
    assert c.post("/school/changes/seen", data={"csrf": csrf}).status_code == 303
    assert "changed its academic calendar" not in c.get("/school").text
    assert "changed its academic calendar" not in c.get("/").text

    # a deadline moving is mentioned too, without dragging in every row
    site.text = page(moved(moved(ROWS, "Spring recess", "Mar 15-21, 2027", "Mar 20-26, 2028"),
                           "Commencement", "Dec 12-13, 2026", "Dec 10-11, 2027"))  # fmt: skip
    rt.check_school(site.client(), force=True)
    assert sent[-1].body == "Commencement (2026-2027): now Dec 12–13, 2026 (was Dec 11–12, 2026)"


def test_a_new_year_on_the_page_and_terms_that_are_over(store, tmp_path):
    store.update(school_on)
    cfg = store.current()
    memory = SchoolMemory(tmp_path / "school-state.json")
    sc = SchoolCalendar(cfg, memory, today=lambda: date(2027, 9, 1))
    site = Site(page())
    sc.refresh(site.client())
    # the school rolls the table over: 2026-2027 drops off, 2028-2029 is added
    later = [(t, e, c, b, b.replace("2028", "2029").replace("2027", "2028")) for t, e, c, a, b in ROWS]
    site.text = page(later, years=("2027-2028", "2028-2029"))
    sc.refresh(site.client(), force=True)
    assert "Fall 2028 added (Aug 23 – Dec 12, 2028)" in sc.new_changes
    assert "Fall 2026 is no longer listed (it's over, so Kalends keeps it)" in sc.new_changes
    names = [t.name for t in sc.schedule.terms]
    assert names[:3] == ["Fall 2026", "Spring 2027", "Summer 2027"] and "Summer 2029" in names
    assert sc.schedule.label(date(2026, 12, 8)) == "Fall 2026 · Finals"  # still known from last time
    # a different school starts fresh
    store.update(lambda d: d.section("school").update({"calendar_url": "https://other.example.edu/cal"}))
    other = SchoolCalendar(store.current(), memory, today=lambda: date(2027, 9, 1))
    other.refresh(Site(page(later, years=("2027-2028", "2028-2029"))).client())
    assert other.new_changes == [] and "Fall 2026" not in [t.name for t in other.schedule.terms]


# -- the School page ----------------------------------------------------------------------------------


def test_school_page_reviews_what_was_read_and_rows_can_be_ignored(panel):
    rt, c = panel
    rt.apply(rt.store.update(school_on), fetch=False)
    rt.check_school(Site(page()).client(), force=True)
    html_ = c.get("/school").text
    assert "What Kalends read" in html_ and "a web page" in html_
    assert (
        "Corrected 2 likely typos" in html_
        and "the page says May 31, 2026; Kalends uses May 31, 2027" in html_
    )
    assert "Classes held (not a day off)" in html_ and "Shorter session (not used)" in html_
    assert "No date given" in html_ and "Exam week" in html_
    csrf, _ = csrf_rev(c, "/school")
    key = "Spring | President's Day observance (School of Law classes held) | G/U"
    r = c.post("/school/ignore", data={"csrf": csrf, "key": key})
    assert r.headers["location"] == "/school?review=open&ok=saved#review"
    assert rt.cfg.school.ignore == [key]
    spring = next(t for t in rt.kalends.school.schedule.terms if t.name == "Spring 2027")
    assert "President's Day" not in {p.name for p in spring.periods}
    after = c.get("/school?review=open").text
    assert (
        "Use again" in after and "You're ignoring 2 dates" in after and '<details class="help" open>' in after
    )
    assert rt.kalends.school.memory.latest() is None  # ignoring a row isn't the school changing anything
    # keys that aren't on the page are refused; "use again" brings it back
    c.post("/school/ignore", data={"csrf": csrf, "key": "made up"})
    assert rt.cfg.school.ignore == [key]
    c.post("/school/ignore", data={"csrf": csrf, "key": key, "action": "use"})
    assert rt.cfg.school.ignore == []
    spring = next(t for t in rt.kalends.school.schedule.terms if t.name == "Spring 2027")
    assert "President's Day" in {p.name for p in spring.periods}


def test_check_link_and_save_from_the_panel(panel, monkeypatch):
    rt, c = panel
    site = Site(page())
    real = httpx.Client
    fake = types.SimpleNamespace(**{k: getattr(httpx, k) for k in dir(httpx) if not k.startswith("__")})
    fake.Client = lambda **kw: real(transport=httpx.MockTransport(site))
    from kalends.web import admin as admin_mod

    monkeypatch.setattr(admin_mod, "httpx", fake)
    csrf, rev = csrf_rev(c, "/school")
    html_ = c.post("/school/test", data={"csrf": csrf, "calendar_url": URL}).text
    assert "That link works" in html_ and "a web page" in html_ and "It found 6 terms" in html_
    assert "Kalends corrected 2 likely typos" in html_ and "4 rows had no date yet" in html_
    r = c.post(
        "/school",
        data={"csrf": csrf, "rev": rev, "enabled": "on", "name": "Example University", "calendar_url": URL,
              "pause_classes": "on", "show_viewers": "on"},
    )  # fmt: skip
    assert r.status_code == 303 and site.hits == 2
    assert rt.cfg.school.calendar_url == URL and len(rt.kalends.school.feed_terms) == 6
    assert re.search(
        r"Checked .*a web page .*Academic Calendar \| Example University.* with 78 dates",
        c.get("/school").text,
        re.S,
    )


def test_redirects_keep_their_section(panel):
    rt, c = panel
    csrf, _ = csrf_rev(c, "/school")
    r = c.post(
        "/school/terms/add",
        data={"csrf": csrf, "name": "Fall 2026", "start": "2026-08-24", "end": "2026-12-13"},
    )
    assert r.headers["location"] == "/school?ok=saved#terms"


def test_ignore_list_is_tidied(store):
    store.update(
        lambda d: d.section("school").update(
            {"ignore": [" Fall |  Commencement ", "Fall | Commencement", ""]}
        )
    )
    assert store.current().school.ignore == ["Fall | Commencement"]
