"""Macro-economic calendar cache, official-source adapters, and reminders.

All timestamps are timezone-aware UTC internally.  The service owns the only
network worker; Flask callers consume :meth:`snapshot` and never perform I/O.
ISM dates intentionally account for weekends only because ISM does not publish
an easily machine-readable federal-holiday adjustment calendar.
"""
from __future__ import annotations

import copy
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
DOL_CLAIMS_URL = "https://www.dol.gov/ui/data.pdf"
ISM_CALENDAR_URL = "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"
CENSUS_CALENDAR_URL = "https://www.census.gov/economic-indicators/calendar-listview.html"
TE_CALENDAR_URL = "https://api.tradingeconomics.com/calendar/country/united%20states"

_MONTHS = {
    name: number for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"), 1
    )
}
_USER_AGENT = "TradingBot-MacroCalendar/1.0 (+official-public-schedules)"


class MacroCalendarError(RuntimeError):
    """Calendar configuration or source parsing error."""


class _VisibleTextParser(HTMLParser):
    """Extract visible-ish text without third-party HTML dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._suppressed += 1
        elif tag.lower() in {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._suppressed:
            self._suppressed -= 1
        elif tag.lower() in {"p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))


def html_text(raw: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(raw or "")
    return parser.text()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise MacroCalendarError("Naive datetime is not allowed")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _https_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
        return text if parsed.scheme == "https" and parsed.netloc else None
    except ValueError:
        return None


def _event_id(category: str, scheduled: datetime, provider_id: Optional[str] = None) -> str:
    seed = f"{provider_id or ''}|{category}|{_iso(scheduled)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "-", "--"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    result = float(match.group(0))
    suffix = text[match.end():].strip().upper()
    if suffix.startswith("K"):
        result *= 1_000
    elif suffix.startswith("M"):
        result *= 1_000_000
    elif suffix.startswith("B"):
        result *= 1_000_000_000
    return result


def scenarios_for(category: str, title: str = "") -> Dict[str, Dict[str, str]]:
    """Return cautious, rule-based BTC scenarios; never a price prediction."""
    cat = category.upper()
    watch = "Theo dõi DXY và lợi suất trái phiếu Mỹ; xu hướng thường gặp, không đảm bảo."
    inflation = cat in {"CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI"}
    inverse_jobs = cat in {"UNEMPLOYMENT", "JOBLESS_CLAIMS"}
    growth = cat in {"NFP", "GDP", "RETAIL_SALES", "ISM_MANUFACTURING", "ISM_SERVICES", "JOLTS"}
    if inflation:
        return {
            "higher": {"label": "Actual > Forecast", "btc_direction": "BEARISH",
                       "rationale": "Lạm phát cao hơn consensus thường làm kỳ vọng chính sách hawkish hơn; lợi suất/DXY có thể tăng và BTC chịu áp lực.", "watch": watch},
            "lower": {"label": "Actual < Forecast", "btc_direction": "BULLISH",
                      "rationale": "Lạm phát thấp hơn consensus thường hỗ trợ kỳ vọng dovish; lợi suất/DXY có thể giảm và BTC được hỗ trợ.", "watch": watch},
        }
    if cat == "FOMC_RATE":
        return {
            "higher": {"label": "Lãi suất / thông điệp hawkish hơn kỳ vọng", "btc_direction": "BEARISH",
                       "rationale": "Chính sách thắt chặt hơn kỳ vọng thường hỗ trợ DXY/lợi suất và gây áp lực lên tài sản rủi ro.", "watch": watch},
            "lower": {"label": "Lãi suất / thông điệp dovish hơn kỳ vọng", "btc_direction": "BULLISH",
                      "rationale": "Chính sách nới lỏng hơn kỳ vọng thường hỗ trợ thanh khoản và tài sản rủi ro.", "watch": watch},
        }
    if cat == "FED_PRESS_CONFERENCE":
        return {
            "higher": {"label": "Giọng điệu hawkish", "btc_direction": "BEARISH",
                       "rationale": "Nhấn mạnh lạm phát hoặc duy trì lãi suất cao lâu hơn có thể đẩy DXY/lợi suất lên.", "watch": watch},
            "lower": {"label": "Giọng điệu dovish", "btc_direction": "BULLISH",
                      "rationale": "Tín hiệu sẵn sàng nới lỏng có thể hỗ trợ tài sản rủi ro; cần theo dõi phản ứng DXY/lợi suất.", "watch": watch},
        }
    if inverse_jobs:
        return {
            "higher": {"label": "Actual > Forecast", "btc_direction": "MIXED",
                       "rationale": "Thị trường lao động yếu hơn có thể tạo kỳ vọng dovish và hỗ trợ BTC ban đầu, nhưng rủi ro suy thoái có thể đảo chiều phản ứng.", "watch": watch},
            "lower": {"label": "Actual < Forecast", "btc_direction": "BEARISH",
                      "rationale": "Lao động khỏe hơn thường củng cố kỳ vọng hawkish, có thể đẩy lợi suất/DXY lên và gây áp lực BTC.", "watch": watch},
        }
    if growth:
        return {
            "higher": {"label": "Actual > Forecast", "btc_direction": "BEARISH",
                       "rationale": "Tăng trưởng/lao động mạnh hơn consensus thường nâng kỳ vọng hawkish và lợi suất; BTC có thể giảm ban đầu.", "watch": watch},
            "lower": {"label": "Actual < Forecast", "btc_direction": "MIXED",
                      "rationale": "Dữ liệu yếu hơn có thể hỗ trợ kỳ vọng dovish và BTC ban đầu, nhưng rủi ro suy thoái khiến phản ứng có thể trái chiều.", "watch": watch},
        }
    return {
        "higher": {"label": "Kết quả mạnh hơn kỳ vọng", "btc_direction": "MIXED",
                   "rationale": "Phản ứng phụ thuộc bối cảnh chính sách và định vị thị trường.", "watch": watch},
        "lower": {"label": "Kết quả yếu hơn kỳ vọng", "btc_direction": "MIXED",
                  "rationale": "Phản ứng phụ thuộc bối cảnh chính sách và định vị thị trường.", "watch": watch},
    }


def _surprise(category: str, actual: Any, forecast: Any) -> Tuple[Optional[float], Optional[str]]:
    a, f = _number(actual), _number(forecast)
    if a is None or f is None:
        return None, None
    delta = a - f
    if abs(delta) < 1e-12:
        return 0.0, "INLINE"
    key = "higher" if delta > 0 else "lower"
    direction = scenarios_for(category)[key]["btc_direction"]
    return delta, direction


def _canonical_event(
    *, category: str, title: str, scheduled: datetime, provider: str,
    source: str, source_url: str, schedule_method: str, fetched_at: datetime,
    subtitle: Optional[str] = None, impact: str = "HIGH", provider_id: Optional[str] = None,
    actual: Any = None, forecast: Any = None, previous: Any = None, unit: Optional[str] = None,
    timing_confirmed: bool = True, status: Optional[str] = None,
) -> Dict[str, Any]:
    if scheduled.tzinfo is None:
        raise MacroCalendarError(f"{title}: scheduled time is not timezone-aware")
    scheduled = scheduled.astimezone(timezone.utc)
    now = fetched_at.astimezone(timezone.utc)
    status = status or ("released" if scheduled <= now else "upcoming")
    surprise, direction = _surprise(category, actual, forecast)
    return {
        "event_id": _event_id(category, scheduled, provider_id),
        "category": category,
        "title": title,
        "subtitle": subtitle,
        "impact": impact,
        "scheduled_at_utc": _iso(scheduled),
        "scheduled_at_vn": None,
        "source": source,
        "provider": provider,
        "source_url": _https_url(source_url),
        "schedule_method": schedule_method,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "unit": unit,
        "timing_confirmed": bool(timing_confirmed),
        "status": status,
        "fetched_at": _iso(fetched_at),
        "scenarios": scenarios_for(category, title),
        "surprise": surprise,
        "surprise_direction": direction,
    }


def _unfold_ics(raw: str) -> List[str]:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_ics_dt(key: str, value: str) -> Tuple[datetime, bool]:
    params: Dict[str, str] = {}
    for token in key.split(";")[1:]:
        if "=" in token:
            name, val = token.split("=", 1)
            params[name.upper()] = val
    value = value.strip()
    date_only = params.get("VALUE", "").upper() == "DATE" or len(value) == 8
    fmt = "%Y%m%d" if date_only else ("%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M")
    parsed = datetime.strptime(value.rstrip("Z"), fmt)
    if value.endswith("Z"):
        aware = parsed.replace(tzinfo=timezone.utc)
    else:
        tz_name = params.get("TZID")
        if not tz_name:
            if date_only:
                tz_name = "America/New_York"
            else:
                raise MacroCalendarError(f"ICS DTSTART missing TZID: {key}:{value}")
        # BLS currently emits the legacy IANA link ``US-Eastern``. Resolve it
        # to the canonical zone explicitly so minimal tzdata installations work.
        tz_name = {"US-EASTERN": "America/New_York"}.get(tz_name.strip('"').upper(), tz_name.strip('"'))
        try:
            aware = parsed.replace(tzinfo=ZoneInfo(tz_name))
        except ZoneInfoNotFoundError as exc:
            raise MacroCalendarError(f"Timezone database unavailable for {tz_name}") from exc
    return aware.astimezone(timezone.utc), not date_only


def parse_bls_ics(raw: str, fetched_at: datetime) -> List[Dict[str, Any]]:
    events: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in _unfold_ics(raw):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value.replace("\\,", ",").replace("\\n", " ").strip()

    output: List[Dict[str, Any]] = []
    for item in events:
        summary_key = next((k for k in item if k.upper() == "SUMMARY"), "")
        dt_key = next((k for k in item if k.upper().startswith("DTSTART")), "")
        summary = item.get(summary_key, "")
        if not summary or not dt_key:
            continue
        scheduled, confirmed = _parse_ics_dt(dt_key, item[dt_key])
        if scheduled < fetched_at.astimezone(timezone.utc) - timedelta(days=45):
            continue
        uid = item.get("UID")
        specs: List[Tuple[str, str, Optional[str]]] = []
        lower = summary.lower()
        if "consumer price index" in lower:
            specs = [("CPI", "Consumer Price Index (CPI)", "Headline CPI"),
                     ("CORE_CPI", "Core Consumer Price Index", "CPI excluding food and energy; same BLS release")]
        elif "producer price index" in lower:
            specs = [("PPI", "Producer Price Index (PPI)", summary)]
        elif "employment situation" in lower:
            specs = [
                ("NFP", "Nonfarm Payrolls (NFP)", "Employment Situation release; payroll growth scenario"),
                ("UNEMPLOYMENT", "U.S. Unemployment Rate", "Employment Situation release; inverse labor-market scenario"),
            ]
        elif "job openings and labor turnover" in lower:
            specs = [("JOLTS", "JOLTS Job Openings", "Job Openings and Labor Turnover Survey")]
        for category, title, subtitle in specs:
            output.append(_canonical_event(
                category=category, title=title, subtitle=subtitle, scheduled=scheduled,
                provider="BLS", provider_id=f"{uid or summary}:{category}", source="U.S. Bureau of Labor Statistics",
                source_url=BLS_ICS_URL, schedule_method="official", fetched_at=fetched_at,
                timing_confirmed=confirmed,
                impact="MEDIUM" if category == "JOLTS" else "HIGH",
            ))
    return output


def _year_blocks(text: str) -> Iterable[Tuple[int, str]]:
    matches = list(re.finditer(r"(?:Year\s+|\b)(20\d{2})(?=\s+(?:FOMC\s+Meetings|Release|January|February|March|April|May|June|July|August|September|October|November|December))", text, re.I))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield int(match.group(1)), text[match.end():end]


def _et_datetime(year: int, month_name: str, day: int, clock: str, ampm: str) -> datetime:
    try:
        eastern = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise MacroCalendarError("Timezone database unavailable for America/New_York") from exc
    parsed_time = datetime.strptime(f"{clock} {ampm.upper()}", "%I:%M %p").time()
    return datetime.combine(date(year, _MONTHS[month_name.title()], day), parsed_time, eastern).astimezone(timezone.utc)


def parse_bea_schedule(raw: str, fetched_at: datetime) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", html_text(raw)).strip()
    pattern = re.compile(
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(?P<day>\d{1,2})\s+(?P<clock>\d{1,2}:\d{2})\s*(?P<ampm>AM|PM)\s+"
        r"(?:News|Data)?\s*(?P<title>Personal Income and Outlays[^|]*?|GDP\s*\([^|]*?|GDP\s+by[^|]*?)"
        r"(?=(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s+\d{1,2}:\d{2}|To Be Announced|$)", re.I,
    )
    output: List[Dict[str, Any]] = []
    blocks = list(_year_blocks(text))
    if not blocks:
        years = sorted({fetched_at.year, fetched_at.year + 1})
        blocks = [(year, text) for year in years[:1]]
    for year, block in blocks:
        for match in pattern.finditer(block):
            release = re.sub(r"\s+", " ", match.group("title")).strip(" ,-")
            if not (release.lower().startswith("personal income and outlays") or re.match(r"^gdp\s*\(", release, re.I)):
                continue
            scheduled = _et_datetime(year, match.group("month"), int(match.group("day")), match.group("clock"), match.group("ampm"))
            if release.lower().startswith("personal"):
                specs = [("PCE", "PCE Price Index", "Personal Income and Outlays — headline PCE"),
                         ("CORE_PCE", "Core PCE Price Index", "PCE excluding food and energy; same BEA release")]
            else:
                specs = [("GDP", "U.S. GDP", release)]
            for category, title, subtitle in specs:
                output.append(_canonical_event(
                    category=category, title=title, subtitle=subtitle, scheduled=scheduled,
                    provider="BEA", provider_id=f"BEA:{release}:{category}", source="U.S. Bureau of Economic Analysis",
                    source_url=BEA_SCHEDULE_URL, schedule_method="official", fetched_at=fetched_at,
                ))
    if not output:
        raise MacroCalendarError("BEA schedule contained no supported release dates")
    return output


def parse_fed_calendar(raw: str, fetched_at: datetime) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", html_text(raw)).strip()
    output: List[Dict[str, Any]] = []
    for year, block in _year_blocks(text):
        for match in re.finditer(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2})\s*-\s*(\d{1,2})\*?", block, re.I,
        ):
            month, first, last = match.group(1), int(match.group(2)), match.group(3)
            end_day = int(last)
            meeting_date = date(year, _MONTHS[month.title()], end_day)
            if meeting_date < fetched_at.date() - timedelta(days=45):
                continue
            for category, title, clock, subtitle in (
                ("FOMC_RATE", "FOMC Rate Decision", "2:00", "Meeting end date; 2:00 PM ET is derived from the Fed's standard statement release policy"),
                ("FED_PRESS_CONFERENCE", "Fed Press Conference", "2:30", "Meeting end date; 2:30 PM ET is derived from the Fed's standard press-conference policy"),
            ):
                scheduled = _et_datetime(year, month, end_day, clock, "PM")
                output.append(_canonical_event(
                    category=category, title=title, subtitle=subtitle, scheduled=scheduled,
                    provider="Federal Reserve", provider_id=f"FOMC:{meeting_date.isoformat()}:{category}",
                    source="Federal Reserve", source_url=FED_CALENDAR_URL,
                    schedule_method="derived", fetched_at=fetched_at,
                ))
    if not output:
        raise MacroCalendarError("Federal Reserve calendar contained no current/future meetings")
    return output


def recurring_events(fetched_at: datetime, months_ahead: int = 4) -> List[Dict[str, Any]]:
    """Build DOL/ISM recurring dates. ISM adjusts weekends only, not holidays."""
    try:
        eastern = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise MacroCalendarError("Timezone database unavailable for America/New_York") from exc
    local_now = fetched_at.astimezone(eastern)
    end_date = local_now.date() + timedelta(days=max(45, months_ahead * 31))
    output: List[Dict[str, Any]] = []

    day = local_now.date()
    while day <= end_date:
        if day.weekday() == 3:
            scheduled = datetime.combine(day, dt_time(8, 30), eastern).astimezone(timezone.utc)
            output.append(_canonical_event(
                category="JOBLESS_CLAIMS", title="Initial Jobless Claims", subtitle="Weekly Thursday release (official recurring cadence)",
                scheduled=scheduled, provider="DOL", provider_id=f"DOL:claims:{day.isoformat()}",
                source="U.S. Department of Labor", source_url=DOL_CLAIMS_URL,
                schedule_method="recurring", fetched_at=fetched_at, impact="MEDIUM",
            ))
        day += timedelta(days=1)

    year, month = local_now.year, local_now.month
    for _ in range(months_ahead + 1):
        weekdays = [date(year, month, d) for d in range(1, 32)
                    if _valid_date(year, month, d) and date(year, month, d).weekday() < 5]
        for category, title, release_day, ordinal in (
            ("ISM_MANUFACTURING", "ISM Manufacturing PMI", weekdays[0], "first"),
            ("ISM_SERVICES", "ISM Services PMI", weekdays[2], "third"),
        ):
            scheduled = datetime.combine(release_day, dt_time(10, 0), eastern).astimezone(timezone.utc)
            if release_day >= local_now.date() - timedelta(days=7):
                output.append(_canonical_event(
                    category=category, title=title,
                    subtitle=f"{ordinal.capitalize()} business day, weekends-only calculation; federal-holiday adjustments are not modeled",
                    scheduled=scheduled, provider="ISM", provider_id=f"ISM:{category}:{release_day.isoformat()}",
                    source="Institute for Supply Management", source_url=ISM_CALENDAR_URL,
                    schedule_method="recurring", fetched_at=fetched_at, timing_confirmed=False,
                    impact="MEDIUM",
                ))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return output


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def parse_census_calendar(raw: str, fetched_at: datetime) -> List[Dict[str, Any]]:
    """Conservatively parse only explicitly dated Retail Sales releases."""
    text = re.sub(r"\s+", " ", html_text(raw)).strip()
    output: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"(?:Advance Monthly Sales for Retail and Food Services|Retail Sales).{0,180}?"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2}),?\s+(20\d{2})(?:.{0,40}?(\d{1,2}:\d{2})\s*(AM|PM))?", re.I,
    )
    for match in pattern.finditer(text):
        if not match.group(4):
            continue
        scheduled = _et_datetime(int(match.group(3)), match.group(1), int(match.group(2)), match.group(4), match.group(5))
        output.append(_canonical_event(
            category="RETAIL_SALES", title="U.S. Retail Sales", subtitle="Advance Monthly Sales for Retail and Food Services",
            scheduled=scheduled, provider="Census", provider_id=f"Census:retail:{scheduled.date().isoformat()}",
            source="U.S. Census Bureau", source_url=CENSUS_CALENDAR_URL,
            schedule_method="official", fetched_at=fetched_at,
        ))
    if not output:
        raise MacroCalendarError("Census calendar unavailable or no explicit Retail Sales date/time found")
    return output


def enrich_trading_economics(events: List[Dict[str, Any]], payload: Any) -> List[Dict[str, Any]]:
    """Merge optional TE actual/forecast/previous fields into official events only."""
    if not isinstance(payload, list):
        raise MacroCalendarError("Trading Economics response is not a list")
    aliases = {
        "CPI": ("consumer price", "inflation rate"), "CORE_CPI": ("core consumer price", "core inflation"),
        "PPI": ("producer price",), "PCE": ("pce price", "personal consumption"),
        "CORE_PCE": ("core pce",), "NFP": ("non farm payroll", "nonfarm payroll"),
        "UNEMPLOYMENT": ("unemployment rate",), "JOLTS": ("jolts", "job openings"),
        "GDP": ("gdp",), "RETAIL_SALES": ("retail sales",), "JOBLESS_CLAIMS": ("jobless claims", "initial claims"),
        "ISM_MANUFACTURING": ("ism manufacturing",), "ISM_SERVICES": ("ism services", "ism non-manufacturing"),
        "FOMC_RATE": ("interest rate decision", "fed interest rate"),
    }
    candidates: List[Tuple[Dict[str, Any], datetime, str]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        when = _parse_iso(row.get("Date"))
        name = str(row.get("Event") or "").lower()
        if when and name:
            candidates.append((row, when, name))
    enriched = copy.deepcopy(events)
    for event in enriched:
        scheduled = _parse_iso(event.get("scheduled_at_utc"))
        if not scheduled:
            continue
        names = aliases.get(event.get("category"), ())
        matches = [(row, abs((when - scheduled).total_seconds())) for row, when, name in candidates
                   if any(alias in name for alias in names) and abs((when.date() - scheduled.date()).days) <= 1]
        if not matches:
            continue
        row = min(matches, key=lambda pair: pair[1])[0]
        for key, te_key in (("actual", "Actual"), ("forecast", "Forecast"), ("previous", "Previous")):
            value = row.get(te_key)
            event[key] = None if value in (None, "") else value
        if row.get("Unit") not in (None, ""):
            event["unit"] = row.get("Unit")
        source_url = _https_url(row.get("SourceURL"))
        if source_url:
            event["enrichment_source_url"] = source_url
        event["enrichment_provider"] = "Trading Economics"
        event["enrichment_date"] = row.get("Date")
        event["enrichment_event"] = row.get("Event")
        event["enrichment_importance"] = row.get("Importance")
        surprise, direction = _surprise(event["category"], event.get("actual"), event.get("forecast"))
        event["surprise"], event["surprise_direction"] = surprise, direction
        if event.get("actual") is not None:
            event["status"] = "released"
    return enriched


class MacroCalendarService:
    """Independent daemon service with immutable copy-on-write snapshots."""

    def __init__(self, config: Any, notifier: Any, *, now_fn: Optional[Callable[[], datetime]] = None,
                 http_get: Optional[Callable[..., Any]] = None) -> None:
        self.config = config
        self.notifier = notifier
        self.enabled = bool(getattr(config, "MACRO_CALENDAR_ENABLED", True))
        self.refresh_seconds = max(60, int(getattr(config, "MACRO_CALENDAR_REFRESH_SECONDS", 21600)))
        self.stale_seconds = max(60, int(getattr(config, "MACRO_CALENDAR_STALE_SECONDS", 43200)))
        self.reminder_enabled = bool(getattr(config, "MACRO_CALENDAR_REMINDER_ENABLED", True))
        self.reminder_hours = max(0, float(getattr(config, "MACRO_CALENDAR_REMINDER_HOURS", 24)))
        self.reminder_window = max(60, int(getattr(config, "MACRO_CALENDAR_REMINDER_WINDOW_SECONDS", 3600)))
        self.reminder_only_high = bool(getattr(config, "MACRO_CALENDAR_REMINDER_ONLY_HIGH", True))
        configured_cache = str(
            getattr(config, "MACRO_CALENDAR_CACHE_FILE", "logs/macro_calendar.json")
        )
        self.cache_path = (
            configured_cache if os.path.isabs(configured_cache)
            else os.path.join(os.path.dirname(os.path.abspath(__file__)), configured_cache)
        )
        self.te_key = str(getattr(config, "TRADING_ECONOMICS_API_KEY", "") or os.environ.get("TRADING_ECONOMICS_API_KEY", "")).strip()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._http_get = http_get or requests.get
        self._lock = threading.RLock()
        self._refresh_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reminder_thread: Optional[threading.Thread] = None
        self._refreshing = False
        self._data = self._empty_data()
        self._timezone_error: Optional[str] = None
        try:
            self._vn_tz = ZoneInfo(str(getattr(config, "MACRO_CALENDAR_TIMEZONE", "Asia/Ho_Chi_Minh")))
            ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError as exc:
            self._vn_tz = None
            self._timezone_error = f"Timezone database unavailable: {exc}"
            self._data["error"] = self._timezone_error
        self._load_cache()

    def _empty_data(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "events": [], "reminders": {},
            "fetched_at": None, "last_success_at": None, "source_errors": {},
            "refreshing": False, "error": None,
        }

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            raise MacroCalendarError("Clock returned a naive datetime")
        return value.astimezone(timezone.utc)

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("schema_version") != SCHEMA_VERSION or not isinstance(loaded.get("events"), list):
                raise ValueError("unsupported cache schema")
            loaded.setdefault("reminders", {})
            loaded.setdefault("source_errors", {})
            loaded["refreshing"] = False
            if self._timezone_error:
                loaded["error"] = self._timezone_error
            with self._lock:
                self._data = loaded
        except FileNotFoundError:
            return
        except Exception as exc:
            corrupt = f"{self.cache_path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.cache_path, corrupt)
            except OSError:
                pass
            logger.warning("[MacroCalendar] Corrupt cache backed up: %s", exc)

    def _persist_locked(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        tmp = f"{self.cache_path}.tmp"
        payload = copy.deepcopy(self._data)
        payload["refreshing"] = False
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.cache_path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._data)
            result["refreshing"] = self._refreshing
        now = self._now()
        fetched = _parse_iso(result.get("last_success_at") or result.get("fetched_at"))
        age = int((now - fetched).total_seconds()) if fetched else None
        result["age_seconds"] = max(0, age) if age is not None else None
        result["stale"] = age is None or age > self.stale_seconds
        result["enabled"] = self.enabled
        result["timezone"] = str(getattr(self.config, "MACRO_CALENDAR_TIMEZONE", "Asia/Ho_Chi_Minh"))
        result["reminder"] = {
            "enabled": self.reminder_enabled, "hours": self.reminder_hours,
            "window_seconds": self.reminder_window, "only_high": self.reminder_only_high,
        }
        result.pop("reminders", None)
        return result

    def start(self) -> Optional[threading.Thread]:
        if not self.enabled or self._timezone_error:
            return None
        with self._lock:
            self._stop_event.clear()
            if self._thread is None or not self._thread.is_alive():
                self._refresh_event.set()
                self._thread = threading.Thread(target=self._run, name="macro-calendar-refresh", daemon=True)
                self._thread.start()
            if self._reminder_thread is None or not self._reminder_thread.is_alive():
                self._reminder_thread = threading.Thread(
                    target=self._run_reminders, name="macro-calendar-reminders", daemon=True
                )
                self._reminder_thread.start()
            return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._refresh_event.set()
        for thread in (self._thread, self._reminder_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=timeout)

    def request_refresh(self) -> bool:
        if not self.enabled or self._timezone_error:
            return False
        already = self._refresh_event.is_set() or self._refreshing
        self._refresh_event.set()
        return not already

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = self._now()
            with self._lock:
                last = _parse_iso(self._data.get("fetched_at"))
            due = not last or (now - last).total_seconds() >= self.refresh_seconds
            if due or self._refresh_event.is_set():
                self._refresh_event.clear()
                try:
                    self.refresh_now()
                except Exception:
                    logger.exception("[MacroCalendar] Refresh cycle failed")
            self._refresh_event.wait(min(60, max(1, self.refresh_seconds)))

    def _run_reminders(self) -> None:
        """Poll independently so slow provider refreshes never delay reminders."""
        while not self._stop_event.is_set():
            try:
                self.process_reminders()
            except Exception:
                logger.exception("[MacroCalendar] Reminder cycle failed")
            self._stop_event.wait(60)

    def _get_text(self, url: str) -> str:
        response = self._http_get(url, timeout=20, allow_redirects=True, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
        return response.text

    def refresh_now(self) -> Dict[str, Any]:
        if self._timezone_error:
            return self.snapshot()
        with self._lock:
            if self._refreshing:
                return self.snapshot()
            self._refreshing = True
        now = self._now()
        adapters = {
            "BLS": lambda: parse_bls_ics(self._get_text(BLS_ICS_URL), now),
            "BEA": lambda: parse_bea_schedule(self._get_text(BEA_SCHEDULE_URL), now),
            "Federal Reserve": lambda: parse_fed_calendar(self._get_text(FED_CALENDAR_URL), now),
            "DOL/ISM recurring": lambda: recurring_events(now),
            "Census": lambda: parse_census_calendar(self._get_text(CENSUS_CALENDAR_URL), now),
        }
        with self._lock:
            old_events = copy.deepcopy(self._data.get("events", []))
        successful: Dict[str, List[Dict[str, Any]]] = {}
        errors: Dict[str, str] = {}
        try:
            for provider, adapter in adapters.items():
                try:
                    successful[provider] = adapter()
                except Exception as exc:
                    errors[provider] = f"{type(exc).__name__}: {exc}"
                    logger.warning("[MacroCalendar] %s refresh failed: %s", provider, exc)

            replaced_providers = {"BLS", "BEA", "Federal Reserve", "DOL", "ISM", "Census"}
            succeeded_providers = set()
            for key, values in successful.items():
                if key == "DOL/ISM recurring":
                    succeeded_providers.update({"DOL", "ISM"})
                elif values:
                    succeeded_providers.add(key)
            merged = [event for event in old_events
                      if event.get("provider") not in replaced_providers or event.get("provider") not in succeeded_providers]
            for values in successful.values():
                merged.extend(values)
            by_id = {event["event_id"]: event for event in merged if event.get("event_id")}
            events = sorted(by_id.values(), key=lambda event: event.get("scheduled_at_utc", ""))

            if self.te_key:
                try:
                    te_response = self._http_get(
                        TE_CALENDAR_URL, params={"c": self.te_key, "d1": now.date().isoformat(),
                                                 "d2": (now.date() + timedelta(days=150)).isoformat()},
                        timeout=20, headers={"User-Agent": _USER_AGENT},
                    )
                    te_response.raise_for_status()
                    events = enrich_trading_economics(events, te_response.json())
                except Exception as exc:
                    errors["Trading Economics"] = f"{type(exc).__name__}: {exc}"

            for event in events:
                scheduled = _parse_iso(event.get("scheduled_at_utc"))
                if scheduled and self._vn_tz:
                    event["scheduled_at_vn"] = scheduled.astimezone(self._vn_tz).isoformat()
                if event.get("actual") is not None:
                    event["status"] = "released"
                elif scheduled and scheduled <= now:
                    event["status"] = "released"
                elif not event.get("timing_confirmed", True):
                    event["status"] = "tentative"
                else:
                    event["status"] = "upcoming"

            any_success = any(successful.values())
            with self._lock:
                # A reminder may have been claimed/sent while network adapters
                # were running; merge the current ledger rather than the stale
                # refresh-start copy.
                current_reminders = copy.deepcopy(self._data.get("reminders", {}))
                self._data = {
                    "schema_version": SCHEMA_VERSION, "events": events, "reminders": current_reminders,
                    "fetched_at": _iso(now),
                    "last_success_at": _iso(now) if any_success else self._data.get("last_success_at"),
                    "source_errors": errors, "refreshing": False,
                    "error": None if any_success else "Không thể cập nhật nguồn; đang giữ dữ liệu tốt gần nhất",
                }
                self._persist_locked()
        finally:
            with self._lock:
                self._refreshing = False
        return self.snapshot()

    def process_reminders(self) -> int:
        if not self.enabled or not self.reminder_enabled:
            return 0
        now = self._now()
        with self._lock:
            events = copy.deepcopy(self._data.get("events", []))
        sent_count = 0
        for event in events:
            if self.reminder_only_high and event.get("impact") != "HIGH":
                continue
            scheduled = _parse_iso(event.get("scheduled_at_utc"))
            if not scheduled:
                continue
            target = scheduled - timedelta(hours=self.reminder_hours)
            if not (target <= now < target + timedelta(seconds=self.reminder_window)):
                continue
            key = f"{event.get('event_id')}|{_iso(scheduled)}|{self.reminder_hours:g}h"
            should_send = False
            with self._lock:
                record = self._data.setdefault("reminders", {}).get(key, {})
                status = record.get("status")
                attempts = int(record.get("attempt", 0) or 0)
                claimed_at = _parse_iso(record.get("claimed_at"))
                if status == "sent" or attempts >= 3:
                    continue
                if status == "claimed" and claimed_at and (now - claimed_at) < timedelta(hours=2):
                    continue
                self._data["reminders"][key] = {
                    **record, "status": "claimed", "attempt": attempts + 1,
                    "claimed_at": _iso(now), "scheduled_at_utc": _iso(scheduled),
                }
                self._persist_locked()  # durable write-ahead claim
                should_send = True
            if not should_send:
                continue
            message_id = 0
            try:
                message_id = int(self.notifier.telegram.send(self._reminder_message(event, scheduled, now)) or 0)
            except Exception as exc:
                logger.warning("[MacroCalendar] Telegram send raised: %s", exc)
            finished = self._now()
            with self._lock:
                record = self._data["reminders"][key]
                if message_id:
                    record.update({"status": "sent", "sent_at": _iso(finished), "message_id": message_id})
                    sent_count += 1
                else:
                    record.update({"status": "failed", "failed_at": _iso(finished), "message_id": None})
                self._persist_locked()
        return sent_count

    def _reminder_message(self, event: Dict[str, Any], scheduled: datetime, now: datetime) -> str:
        vn = scheduled.astimezone(self._vn_tz) if self._vn_tz else scheduled
        countdown = max(0, int((scheduled - now).total_seconds()))
        hours, remainder = divmod(countdown, 3600)
        minutes = remainder // 60
        scenarios = event.get("scenarios") or {}

        def value(name: str) -> str:
            raw = event.get(name)
            if raw is None or raw == "":
                return "Chưa có consensus" if name == "forecast" else "Chưa có"
            unit = str(event.get("unit") or "")
            text = str(raw)
            return text if unit and text.strip().endswith(unit) else f"{text}{unit}"

        high = scenarios.get("higher", {})
        low = scenarios.get("lower", {})
        source_url = _https_url(event.get("source_url"))
        source_line = f'<a href="{html.escape(source_url, quote=True)}">{html.escape(str(event.get("source") or "Nguồn chính thức"))}</a>' if source_url else html.escape(str(event.get("source") or "Nguồn chính thức"))
        return (
            "📅 <b>NHẮC LỊCH VĨ MÔ — HIGH IMPACT</b>\n"
            f"<b>{html.escape(str(event.get('title') or 'Sự kiện'))}</b>\n"
            f"🇻🇳 {vn.strftime('%d/%m/%Y %H:%M')} (còn {hours}h {minutes}p)\n"
            f"Previous: <b>{html.escape(value('previous'))}</b> · Forecast: <b>{html.escape(value('forecast'))}</b> · Actual: <b>{html.escape(value('actual'))}</b>\n\n"
            f"📉 <b>{html.escape(str(high.get('label', 'Cao hơn kỳ vọng')))}</b> → BTC {html.escape(str(high.get('btc_direction', 'MIXED')))}\n"
            f"{html.escape(str(high.get('rationale', '')))}\n"
            f"📈 <b>{html.escape(str(low.get('label', 'Thấp hơn kỳ vọng')))}</b> → BTC {html.escape(str(low.get('btc_direction', 'MIXED')))}\n"
            f"{html.escape(str(low.get('rationale', '')))}\n\n"
            f"Nguồn: {source_line}\n"
            "⚠️ Xu hướng thường gặp, không đảm bảo; không phải lời khuyên tài chính. Theo dõi DXY và lợi suất Mỹ."
        )
