"""Richer schedules than ``daily`` / ``weekly`` / ``hourly`` / ``poll`` (UiPath Orchestrator "time triggers" with cron + calendars).

New slot types (manifest ``schedule.slots[]``; every one still becomes native Windows Task Scheduler triggers):

    {id: month-end, type: monthly,  time: "18:00", day: last}                # day: 1..31, "last", or a list: [1, 15, last]; months: [1, 4, 7, 10] optional
    {id: bizdays,   type: weekdays, time: "07:30"}                           # Monday-Friday
    {id: office,    type: interval, every_minutes: 15, window: "08:00-18:00", days: [Monday, Tuesday]}   # window/days optional
    {id: quarter,   type: cron,     expr: "0 6 1 1,4,7,10 *"}                # 5 fields: minute hour day-of-month month day-of-week

Cron subset: ``*``, numbers, ``a-b`` ranges, ``a,b`` lists, ``*/n`` and ``a-b/n`` steps, month names (jan..dec), weekday names (sun..sat),
0 or 7 = Sunday. As in classic cron, when BOTH day-of-month and day-of-week are restricted a day matches if EITHER does. A cron slot that fires at
more than 48 distinct times per day is refused (a Task Scheduler task holds a limited number of triggers) - use an ``interval`` slot.

Calendars: ``schedule.blackouts`` lists dates the automation must not run (holidays, freezes)::

    blackouts: ["2026-12-25", {date: "2027-01-01", reason: "New Year"}, {from: "2026-12-28", to: "2026-12-31", reason: "year-end freeze"}]

Blackouts are enforced at run time (the run is recorded as ``skipped_maintenance`` with the blackout as its window), independent of how the
trigger fired. :func:`next_runs` is the pure source of truth for "when does this slot run next" (used by tests, docs and dashboards).

Time zone handling: wall-clock times are in ``schedule.timezone``; Task Scheduler needs the machine's local time, so the generator converts using
today's offset. If that conversion moves a slot to another *calendar day* (e.g. 23:30 in a zone far from the machine), weekly/weekday/cron-weekday
days are shifted accordingly; fixed day-of-month slots cannot be shifted safely and are refused - express them in the machine's zone.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NEW_TYPES = ("monthly", "weekdays", "interval", "cron")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MAX_CRON_TIMES = 48
MAX_BLACKOUTS = 500
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WINDOW = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MON_NAMES = {n[:3].lower(): i + 1 for i, n in enumerate(MONTHS)}
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


# --- cron ------------------------------------------------------------------------------------------------
class CronSpec:
    """Parsed 5-field cron. ``dows`` uses 0 = Sunday .. 6 = Saturday."""

    def __init__(self, minutes, hours, doms, months, dows, dom_star: bool, dow_star: bool):
        self.minutes, self.hours, self.doms, self.months, self.dows = minutes, hours, doms, months, dows
        self.dom_star, self.dow_star = dom_star, dow_star

    def times(self) -> List[Tuple[int, int]]:
        return [(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok, dow_ok = d.day in self.doms, ((d.weekday() + 1) % 7) in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok


def _field(text: str, lo: int, hi: int, names: Optional[Dict[str, int]], what: str) -> Tuple[frozenset, bool]:
    star_only = text == "*"
    out: set = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"cron {what}: empty list item")
        base, _, step_s = part.partition("/")
        step = 1
        if step_s != "" or "/" in part:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"cron {what}: bad step {step_s!r}")
            step = int(step_s)

        def num(tok: str) -> int:
            t = tok.lower()
            if names and t in names:
                return names[t]
            if not tok.isdigit():
                raise ValueError(f"cron {what}: {tok!r} is not a number" + (" or a name" if names else ""))
            return int(tok)

        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            x, _, y = base.partition("-")
            a, b = num(x), num(y)
        else:
            a = num(base)
            b = hi if step_s != "" else a
        if not (lo <= a <= hi) or not (lo <= b <= hi) or a > b:
            raise ValueError(f"cron {what}: {part!r} is outside {lo}-{hi}")
        for v in range(a, b + 1, step):
            out.add(v % 7 if what == "day-of-week" else v)  # 7 is Sunday, like 0
    if not out:
        raise ValueError(f"cron {what}: matches nothing")
    return frozenset(out), star_only


def parse_cron(expr: str) -> CronSpec:
    if not isinstance(expr, str) or len(expr) > 100:
        raise ValueError("cron expression must be a string of at most 100 characters")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron expression needs exactly 5 fields: minute hour day-of-month month day-of-week")
    minutes, _ = _field(fields[0], 0, 59, None, "minute")
    hours, _ = _field(fields[1], 0, 23, None, "hour")
    doms, dom_star = _field(fields[2], 1, 31, None, "day-of-month")
    months, _ = _field(fields[3], 1, 12, _MON_NAMES, "month")
    dows, dow_star = _field(fields[4], 0, 7, _DOW_NAMES, "day-of-week")
    for mine, other in ((fields[2], fields[4]), (fields[4], fields[2])):
        if mine.startswith("*") and mine != "*" and other != "*":
            # classic cron treats "*/n" as "unrestricted" for the day-of-month OR day-of-week rule (AND semantics), other implementations
            # treat it as restricted (OR): refuse the ambiguity instead of firing on the wrong days
            raise ValueError("cron: a stepped */n day-of-month/day-of-week combined with a restricted other day field is ambiguous; "
                             "list the days explicitly or use '*' for one of the two fields")
    if dom_star is False and dow_star:  # only the day-of-month restricts: some listed month must actually have such a day
        longest = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
        if not any(d <= longest[m] for m in months for d in doms):
            raise ValueError("cron: the day-of-month / month combination matches no calendar day (e.g. February 30th)")
    return CronSpec(minutes, hours, doms, months, dows, dom_star, dow_star)


# --- validation ------------------------------------------------------------------------------------------
def _day_list(value: Any, label: str, errs: List[str]) -> List[Any]:
    out = []
    for d in (value if isinstance(value, list) else [value]):
        if isinstance(d, str) and d.capitalize() in DAYS:
            out.append(d.capitalize())
        else:
            errs.append(f"{label}: bad day {d!r}")
    return out


def _month_days(slot: dict, errs: List[str], label: str) -> List[Any]:
    raw = slot.get("day")
    if raw is None or raw == []:
        errs.append(f"{label}: monthly slot needs day")
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: List[Any] = []
    for d in items:
        if isinstance(d, str) and d.strip().lower() == "last":
            out.append("last")
        elif isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 31:
            out.append(d)
        else:
            errs.append(f"{label}: monthly day must be 1-31 or 'last', got {d!r}")
    if not out and not errs:
        errs.append(f"{label}: monthly slot needs day")
    return out


def validate_slot(slot: dict, label: str = "slot") -> List[str]:
    """Problems with a NEW-type slot (daily/weekly/hourly/poll are validated in manifest.validate). Empty list = fine."""
    errs: List[str] = []
    t = slot.get("type")
    name = f"{label} {slot.get('id')!r}"
    if t in ("monthly", "weekdays") and not _HHMM.match(str(slot.get("time", ""))):
        errs.append(f"{name} needs time \"HH:MM\" (quote it)")
    if t == "monthly":
        _month_days(slot, errs, name)
        for m in slot.get("months") or []:
            if not (isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12):
                errs.append(f"{name}: months must be integers 1-12, got {m!r}")
    elif t == "interval":
        n = slot.get("every_minutes")
        if not (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 1440):
            errs.append(f"{name}: interval needs every_minutes between 1 and 1440")
        if slot.get("window") is not None and not _WINDOW.match(str(slot["window"])):
            errs.append(f"{name}: window must look like \"08:00-18:00\"")
        if slot.get("days") is not None:
            _day_list(slot["days"], name, errs)
    elif t == "cron":
        try:
            spec = parse_cron(str(slot.get("expr", "")))
            if len(spec.times()) > MAX_CRON_TIMES:
                errs.append(f"{name}: cron fires at {len(spec.times())} distinct times per day (max {MAX_CRON_TIMES}); use an interval slot")
        except ValueError as exc:
            errs.append(f"{name}: {exc}")
    return errs


def _blackout_entries(blackouts: Any) -> List[dict]:
    out = []
    for b in blackouts or []:
        if isinstance(b, (str, date)):
            out.append({"from": str(b), "to": str(b)})
        elif isinstance(b, dict):
            if "date" in b:
                out.append({"from": str(b["date"]), "to": str(b["date"]), "reason": b.get("reason")})
            else:
                out.append({"from": str(b.get("from", "")), "to": str(b.get("to", "")), "reason": b.get("reason")})
        else:
            out.append({"from": "", "to": ""})
    return out


def validate_blackouts(blackouts: Any, label: str = "schedule") -> List[str]:
    errs: List[str] = []
    if blackouts is None:
        return errs
    if not isinstance(blackouts, list):
        return [f"{label}: blackouts must be a list"]
    if len(blackouts) > MAX_BLACKOUTS:
        errs.append(f"{label}: at most {MAX_BLACKOUTS} blackout entries")
    for b in _blackout_entries(blackouts[:MAX_BLACKOUTS]):
        try:
            if not (_ISO_DATE.match(b["from"]) and _ISO_DATE.match(b["to"])):
                raise ValueError("dates must be YYYY-MM-DD")
            if date.fromisoformat(b["from"]) > date.fromisoformat(b["to"]):
                raise ValueError("'from' is after 'to'")
        except ValueError as exc:
            errs.append(f"{label}: bad blackout {b.get('from')!r}..{b.get('to')!r}: {exc}")
    return errs


def compile_blackouts(blackouts: Any) -> List[Tuple[date, date, dict]]:
    """Parse the blackout list once (malformed entries are dropped): (from, to, entry) rows for :func:`in_compiled`."""
    out: List[Tuple[date, date, dict]] = []
    if not isinstance(blackouts, list):
        return out
    for b in _blackout_entries(blackouts[:MAX_BLACKOUTS]):
        try:
            out.append((date.fromisoformat(b["from"]), date.fromisoformat(b["to"]), b))
        except (ValueError, TypeError):
            continue
    return out


def in_compiled(rows: List[Tuple[date, date, dict]], day: date) -> Optional[dict]:
    for lo, hi, b in rows:
        if lo <= day <= hi:
            return {"blackout": f"{b['from']}..{b['to']}", "reason": str(b.get("reason") or "blackout date")[:120]}
    return None


def in_blackout(blackouts: Any, day: date) -> Optional[dict]:
    """The blackout entry covering ``day`` (dict with from/to/reason), else None. Malformed entries are ignored (never raise)."""
    return in_compiled(compile_blackouts(blackouts), day)


# --- next run times (pure) -------------------------------------------------------------------------------
def _last_day(d: date) -> int:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def _window_minutes(window: str) -> Tuple[int, int]:
    m = _WINDOW.match(window)
    assert m
    return int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))


def occurrences(slot: dict, day: date) -> List[datetime]:
    """Naive wall-clock datetimes (schedule's zone) of the firings that *start* on ``day``.

    An interval window that crosses midnight (``22:00-02:00``) spills into the next calendar day; days/blackouts of such a slot are judged by
    the firing's own date at run time. Hourly/poll assume their grid restarts at 00:00 each day.
    """
    t = slot.get("type")
    wd = DAYS[day.weekday()]

    def hhmm(s: Any) -> dtime:
        m = _HHMM.match(str(s))
        if not m:
            raise ValueError(f"bad time {s!r}")
        return dtime(int(m.group(1)), int(m.group(2)))

    def grid(start_min: int, stop_min: int, step: int) -> List[datetime]:
        base = datetime.combine(day, dtime(0, 0))
        return [base + timedelta(minutes=m) for m in range(start_min, stop_min, max(step, 1))]

    if t == "daily":
        return [datetime.combine(day, hhmm(slot["time"]))]
    if t == "weekly":
        ok = wd in [str(d).capitalize() for d in (slot.get("days") or ["Monday"])]
        return [datetime.combine(day, hhmm(slot["time"]))] if ok else []
    if t == "weekdays":
        return [datetime.combine(day, hhmm(slot["time"]))] if day.weekday() < 5 else []
    if t == "monthly":
        months = slot.get("months")
        if months and day.month not in months:
            return []
        raw = slot["day"] if isinstance(slot["day"], list) else [slot["day"]]
        want = {_last_day(day) if isinstance(d, str) else d for d in raw}
        return [datetime.combine(day, hhmm(slot["time"]))] if day.day in want else []
    if t == "hourly":
        return grid(0, 24 * 60, int(slot.get("every", 1)) * 60)
    if t == "poll":
        return grid(0, 24 * 60, int(slot.get("every_minutes", 5)))
    if t == "interval":
        n = int(slot["every_minutes"])
        days = slot.get("days")
        if days and wd not in [str(d).capitalize() for d in days]:
            return []
        if not slot.get("window"):
            return grid(0, 24 * 60, n)
        s, e = _window_minutes(str(slot["window"]))
        length = (e - s) if e > s else (24 * 60 - s + e)
        return grid(s, s + length, n)
    if t == "cron":
        spec = parse_cron(str(slot["expr"]))
        return [datetime.combine(day, dtime(h, m)) for h, m in spec.times()] if spec.day_matches(day) else []
    raise ValueError(f"unknown slot type {t!r}")


def next_runs(slot: dict, after: datetime, n: int = 5, tz: Any = None, blackouts: Any = None, horizon_days: int = 800) -> List[datetime]:
    """The next ``n`` UTC datetimes strictly after ``after`` at which ``slot`` fires, skipping blackout dates.

    ``tz`` is the schedule's zone: a tzinfo, an IANA/UTC name, or None for the machine's local zone. Pure: no clock, no I/O.
    """
    n = max(0, min(int(n), 1000))
    if n == 0:
        return []
    tzinfo = _resolve_tz(tz)
    after_utc = after if after.tzinfo else after.replace(tzinfo=timezone.utc)
    start_day = (after_utc.astimezone(tzinfo) if tzinfo else after_utc.astimezone()).date()
    found: List[datetime] = []
    reached = False
    rows = compile_blackouts(blackouts)          # parsed once, not once per occurrence (an every-minute slot x 500 entries x 800 days)
    blocked: Dict[date, bool] = {}
    for i in range(-1, max(1, int(horizon_days))):
        day = start_day + timedelta(days=i)
        for naive in occurrences(slot, day):
            nd = naive.date()
            if nd not in blocked:
                blocked[nd] = in_compiled(rows, nd) is not None
            if blocked[nd]:
                continue
            utc = (naive.replace(tzinfo=tzinfo) if tzinfo else naive.astimezone()).astimezone(timezone.utc)
            if utc > after_utc:
                found.append(utc)
        if len(found) >= n:
            if reached:  # one more day so a midnight-spill firing cannot sort before an already-collected one
                break
            reached = True
    return sorted(found)[:n]


def _resolve_tz(tz: Any):
    if tz is None or hasattr(tz, "utcoffset"):
        return tz
    from .manifest import _tz
    return _tz(str(tz), strict=True)


# --- Task Scheduler XML ---------------------------------------------------------------------------------
def _localize(hhmm: str, tz_name: Optional[str]) -> Tuple[str, int]:
    """Wall time in the schedule's zone -> (machine-local HH:MM, calendar-day shift of that conversion today)."""
    from .manifest import _tz
    h, m = (int(x) for x in hhmm.split(":"))
    tz = _tz(tz_name, strict=True)  # an unresolvable zone raises: a fixed-time task at the wrong wall-clock time is worse than none
    if tz is None:
        return f"{h:02d}:{m:02d}", 0
    there = datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0)
    here = there.astimezone()
    return f"{here.hour:02d}:{here.minute:02d}", (here.date() - there.date()).days


def _shift_days(days: Sequence[str], delta: int) -> List[str]:
    return [DAYS[(DAYS.index(d) + delta) % 7] for d in days]


def _days_xml(days: Sequence[str]) -> str:
    return "<DaysOfWeek>" + "".join(f"<{d} />" for d in days) + "</DaysOfWeek>"


def _months_xml(months: Sequence[int]) -> str:
    return "<Months>" + "".join(f"<{MONTHS[m - 1]} />" for m in sorted(months)) + "</Months>"


def _cal(start_day: str, hhmm: str, body: str, repetition: str = "") -> str:
    return (f"<CalendarTrigger>{repetition}<StartBoundary>{start_day}T{hhmm}:00</StartBoundary><Enabled>true</Enabled>{body}</CalendarTrigger>")


def _iso_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m or not h else "")


def trigger_xml(slot: dict, tz_name: Optional[str], start_day: str) -> str:
    """Task Scheduler ``<Triggers>`` content for a NEW-type slot. Raises ValueError for slots that cannot be expressed safely."""
    t = slot["type"]
    if not (isinstance(start_day, str) and _ISO_DATE.match(start_day)):
        raise ValueError(f"start_day must be YYYY-MM-DD, got {start_day!r}")
    date.fromisoformat(start_day)
    errs = validate_slot(slot)
    if errs:
        raise ValueError("; ".join(errs))
    if t == "weekdays":
        hhmm, delta = _localize(str(slot["time"]), tz_name)
        days = _shift_days(DAYS[:5], delta)
        return _cal(start_day, hhmm, f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval>{_days_xml(days)}</ScheduleByWeek>")
    if t == "monthly":
        hhmm, delta = _localize(str(slot["time"]), tz_name)
        raw = slot["day"] if isinstance(slot["day"], list) else [slot["day"]]
        if delta:
            raise ValueError("monthly slot: the time zone conversion moves the run to another calendar day; use the machine's time zone for fixed days of the month")
        days = "".join("<Day>Last</Day>" if (isinstance(d, str)) else f"<Day>{d}</Day>" for d in raw)
        months = slot.get("months") or list(range(1, 13))
        return _cal(start_day, hhmm, f"<ScheduleByMonth><DaysOfMonth>{days}</DaysOfMonth>{_months_xml(months)}</ScheduleByMonth>")
    if t == "interval":
        n = int(slot["every_minutes"])
        days = [str(d).capitalize() for d in (slot.get("days") or [])]
        if slot.get("window"):
            s, e = _window_minutes(str(slot["window"]))
            length = (e - s) if e > s else (24 * 60 - s + e)
            start_hhmm, delta = _localize(f"{s // 60:02d}:{s % 60:02d}", tz_name)
        else:
            length, start_hhmm, delta = 24 * 60, "00:00", 0
            if tz_name:
                start_hhmm, delta = _localize("00:00", tz_name)
        rep = ""
        if length > n:
            rep = (f"<Repetition><Interval>{_iso_dur(n)}</Interval><Duration>{_iso_dur(length)}</Duration>"
                   "<StopAtDurationEnd>true</StopAtDurationEnd></Repetition>")
        body = (f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval>{_days_xml(_shift_days(days, delta))}</ScheduleByWeek>" if days
                else "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>")
        return _cal(start_day, start_hhmm, body, rep)
    if t == "cron":
        spec = parse_cron(str(slot["expr"]))
        parts: List[str] = []
        months_full = len(spec.months) == 12
        all_dom = "<DaysOfMonth>" + "".join(f"<Day>{d}</Day>" for d in range(1, 32)) + "<Day>Last</Day></DaysOfMonth>"
        for h, m in spec.times():
            hhmm, delta = _localize(f"{h:02d}:{m:02d}", tz_name)
            if spec.dom_star and spec.dow_star:
                body = ("<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>" if months_full
                        else f"<ScheduleByMonth>{all_dom}{_months_xml(sorted(spec.months))}</ScheduleByMonth>")
                parts.append(_cal(start_day, hhmm, body))
                continue
            if not spec.dom_star:
                if delta:
                    raise ValueError("cron slot: the time zone conversion moves a fixed day of the month to another calendar day; use the machine's time zone")
                days = "".join(f"<Day>{d}</Day>" for d in sorted(spec.doms))
                parts.append(_cal(start_day, hhmm, f"<ScheduleByMonth><DaysOfMonth>{days}</DaysOfMonth>{_months_xml(sorted(spec.months))}</ScheduleByMonth>"))
            if not spec.dow_star:
                dow_names = _shift_days([DAYS[(d - 1) % 7] for d in sorted(spec.dows)], delta)
                if months_full:
                    body = f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval>{_days_xml(dow_names)}</ScheduleByWeek>"
                else:
                    weeks = "<Weeks><Week>1</Week><Week>2</Week><Week>3</Week><Week>4</Week><Week>Last</Week></Weeks>"
                    body = f"<ScheduleByMonthDayOfWeek>{weeks}{_days_xml(dow_names)}{_months_xml(sorted(spec.months))}</ScheduleByMonthDayOfWeek>"
                parts.append(_cal(start_day, hhmm, body))
        return "".join(parts)
    raise ValueError(f"unknown slot type {t!r}")
