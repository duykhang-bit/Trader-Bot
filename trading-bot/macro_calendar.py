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
# Free, keyless public consensus feed (current week only, no Actual values).
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CALENDAR_PAGE = "https://www.forexfactory.com/calendar"

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
            "higher": {"label": "Actual > Forecast", "btc_direction": "BULLISH",
                       "rationale": "Thị trường lao động yếu hơn có thể tạo kỳ vọng dovish và hỗ trợ BTC ban đầu, nhưng rủi ro suy thoái có thể đảo chiều phản ứng.", "watch": watch},
            "lower": {"label": "Actual < Forecast", "btc_direction": "BEARISH",
                      "rationale": "Lao động khỏe hơn thường củng cố kỳ vọng hawkish, có thể đẩy lợi suất/DXY lên và gây áp lực BTC.", "watch": watch},
        }
    if growth:
        return {
            "higher": {"label": "Actual > Forecast", "btc_direction": "BEARISH",
                       "rationale": "Tăng trưởng/lao động mạnh hơn consensus thường nâng kỳ vọng hawkish và lợi suất; BTC có thể giảm ban đầu.", "watch": watch},
            "lower": {"label": "Actual < Forecast", "btc_direction": "BULLISH",
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


# category -> (primary FF title regex, [(extra label, FF title regex), ...])
_FF_TITLES: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {
    "CPI": (r"^CPI m/m$", [("CPI y/y", r"^CPI y/y$")]),
    "CORE_CPI": (r"^Core CPI m/m$", [("Core CPI y/y", r"^Core CPI y/y$")]),
    "PPI": (r"^PPI m/m$", [("Core PPI m/m", r"^Core PPI m/m$")]),
    "PCE": (r"^PCE Price Index m/m$", [("PCE y/y", r"^PCE Price Index y/y$")]),
    "CORE_PCE": (r"^Core PCE Price Index m/m$", [("Core PCE y/y", r"^Core PCE Price Index y/y$")]),
    "NFP": (r"^Non-Farm Employment Change$", [("Average Hourly Earnings m/m", r"^Average Hourly Earnings m/m$")]),
    "UNEMPLOYMENT": (r"^Unemployment Rate$", []),
    "JOLTS": (r"^JOLTS Job Openings$", []),
    "GDP": (r"^(Advance |Prelim |Final )?GDP q/q$", [("GDP Price Index q/q", r"^(Advance |Prelim |Final )?GDP Price Index q/q$")]),
    "RETAIL_SALES": (r"^Retail Sales m/m$", [("Core Retail Sales m/m", r"^Core Retail Sales m/m$")]),
    "JOBLESS_CLAIMS": (r"^Unemployment Claims$", []),
    "ISM_MANUFACTURING": (r"^ISM Manufacturing PMI$", [("ISM Manufacturing Prices", r"^ISM Manufacturing Prices$")]),
    "ISM_SERVICES": (r"^ISM Services PMI$", [("ISM Services Prices", r"^ISM Services Prices$")]),
    "FOMC_RATE": (r"^Federal Funds Rate$", []),
}


def enrich_forexfactory(events: List[Dict[str, Any]], payload: Any) -> List[Dict[str, Any]]:
    """Merge public Forex Factory forecast/previous into official USD events.

    The feed only covers the current week and has no Actual values, so only
    events whose official release falls within ±1 day of a feed row are touched.
    """
    if not isinstance(payload, list):
        raise MacroCalendarError("Forex Factory response is not a list")
    rows: List[Tuple[Dict[str, Any], datetime, str]] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("country") != "USD":
            continue
        when = _parse_iso(row.get("date"))
        title = str(row.get("title") or "").strip()
        if when and title:
            rows.append((row, when.astimezone(timezone.utc), title))

    def best(pattern: str, scheduled: datetime) -> Optional[Dict[str, Any]]:
        regex = re.compile(pattern, re.I)
        found = [(row, abs((when - scheduled).total_seconds())) for row, when, title in rows
                 if regex.match(title) and abs((when - scheduled).total_seconds()) <= 36 * 3600]
        return min(found, key=lambda pair: pair[1])[0] if found else None

    def clean(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    enriched = copy.deepcopy(events)
    for event in enriched:
        spec = _FF_TITLES.get(str(event.get("category") or ""))
        scheduled = _parse_iso(event.get("scheduled_at_utc"))
        if not spec or not scheduled:
            continue
        primary = best(spec[0], scheduled)
        if not primary:
            continue
        forecast, previous = clean(primary.get("forecast")), clean(primary.get("previous"))
        if forecast is None and previous is None:
            continue
        event["forecast"], event["previous"] = forecast, previous
        event["consensus_label"] = primary.get("title")
        extras = []
        for label, pattern in spec[1]:
            extra = best(pattern, scheduled)
            if extra and (clean(extra.get("forecast")) or clean(extra.get("previous"))):
                extras.append({"label": label, "forecast": clean(extra.get("forecast")),
                               "previous": clean(extra.get("previous"))})
        event["consensus_extra"] = extras
        event["enrichment_provider"] = "Forex Factory"
        event["enrichment_source_url"] = FF_CALENDAR_PAGE
        event["enrichment_date"] = primary.get("date")
        event["enrichment_event"] = primary.get("title")
    return enriched


KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"
# category -> Kalshi series tickers (first = primary market, rest = extra markets)
KALSHI_SERIES: Dict[str, List[str]] = {
    "FOMC_RATE": ["KXFEDDECISION"],
    "CPI": ["KXCPI", "KXCPIYOY"],
    "CORE_CPI": ["KXCPICORE", "KXCPICOREYOY"],
    "CORE_PCE": ["KXPCECORE"],
    "PPI": ["KXUSPPI"],
    "NFP": ["KXPAYROLLS"],
    "UNEMPLOYMENT": ["KXU3"],
    "GDP": ["KXGDP"],
    "RETAIL_SALES": ["KXUSRETAIL"],
    "JOBLESS_CLAIMS": ["KXJOBLESSCLAIMS"],
    "ISM_MANUFACTURING": ["KXISMPMI"],
}


def _kalshi_prob(market: Dict[str, Any]) -> Optional[float]:
    """Implied YES probability: bid/ask mid when the book is tight, else last trade."""
    bid, ask = _number(market.get("yes_bid_dollars")), _number(market.get("yes_ask_dollars"))
    last = _number(market.get("last_price_dollars"))
    if bid is not None and ask is not None and ask > 0 and ask - bid <= 0.10:
        return round((bid + ask) / 2, 4)
    return last


def _kalshi_summary(event: Dict[str, Any], series: str) -> Optional[Dict[str, Any]]:
    markets = [m for m in event.get("markets") or [] if isinstance(m, dict)]
    rows = []
    for market in markets:
        prob = _kalshi_prob(market)
        if prob is None:
            continue
        rows.append((market, prob))
    if not rows:
        return None
    summary: Dict[str, Any] = {
        "provider": "Kalshi", "series": series, "event_ticker": event.get("event_ticker"),
        "question": event.get("title"), "url": f"https://kalshi.com/markets/{series.lower()}",
    }
    ladder_types = {"greater", "greater_or_equal"}
    is_ladder = (not event.get("mutually_exclusive")
                 and sum(1 for m, _ in rows if m.get("strike_type") in ladder_types) >= 2)
    if not is_ladder:
        outcomes = sorted(({"label": str(m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker")),
                            "prob": p} for m, p in rows), key=lambda o: -o["prob"])
        summary.update({"kind": "outcomes", "outcomes": outcomes[:5]})
        return summary
    # Threshold ladders: only trust strikes with a live, tight order book; stale
    # last trades on illiquid strikes produce impossible (non-monotone) curves.
    ladder = []
    for market in markets:
        strike = _number(market.get("floor_strike"))
        bid, ask = _number(market.get("yes_bid_dollars")), _number(market.get("yes_ask_dollars"))
        if (market.get("strike_type") in ladder_types and strike is not None
                and bid is not None and ask is not None and ask > 0 and ask - bid <= 0.10):
            ladder.append((market, strike, (bid + ask) / 2))
    if len(ladder) < 2 or not any(0.05 <= prob <= 0.95 for _, _, prob in ladder):
        return None
    ladder.sort(key=lambda item: item[1])
    # P(X > s) must be non-increasing in s; clamp residual noise.
    running = 1.0
    monotone = []
    for market, strike, prob in ladder:
        running = min(running, prob)
        monotone.append((market, strike, round(running, 4)))
    strict = ladder[0][0].get("strike_type") == "greater"
    value = lambda m: re.sub(r"^\s*(Above|At least)\s+", "", str(m.get("yes_sub_title") or m.get("floor_strike")), flags=re.I)
    thresholds = [{"label": f"{'>' if strict else '≥'} {value(m)}", "strike": s, "prob": p} for m, s, p in monotone]
    buckets = []
    for (lo, _, p_lo), (hi, _, p_hi) in zip(monotone, monotone[1:]):
        text = f"> {value(lo)} và ≤ {value(hi)}" if strict else f"≥ {value(lo)} và < {value(hi)}"
        buckets.append({"label": text, "prob": round(max(0.0, p_lo - p_hi), 4)})
    summary.update({
        "kind": "ladder", "strict": strict,
        # keep only the informative part of the ladder
        "thresholds": [t for t in thresholds if 0.03 <= t["prob"] <= 0.97] or thresholds[-3:],
        "thresholds_all": thresholds,
        "most_likely": max(buckets, key=lambda b: b["prob"]) if buckets else None,
    })
    return summary


def enrich_kalshi(events: List[Dict[str, Any]], payloads: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Attach prediction-market probabilities (market crowd odds, not advice)."""
    by_series: Dict[str, List[Tuple[Dict[str, Any], datetime]]] = {}
    for series, payload in payloads.items():
        for item in (payload or {}).get("events") or []:
            closes = [_parse_iso(m.get("close_time")) for m in item.get("markets") or [] if isinstance(m, dict)]
            closes = [c for c in closes if c]
            strike = _parse_iso(item.get("strike_date"))
            when = strike or (min(closes) if closes else None)
            if when:
                by_series.setdefault(series, []).append((item, when))
    enriched = copy.deepcopy(events)
    for event in enriched:
        series_list = KALSHI_SERIES.get(str(event.get("category") or ""))
        scheduled = _parse_iso(event.get("scheduled_at_utc"))
        if not series_list or not scheduled:
            continue
        found = []
        for series in series_list:
            options = [(item, abs((when - scheduled).total_seconds())) for item, when in by_series.get(series, [])]
            options = [pair for pair in options if pair[1] <= 36 * 3600]
            if options:
                summary = _kalshi_summary(min(options, key=lambda pair: pair[1])[0], series)
                if summary:
                    found.append(summary)
        if not found:
            continue
        event.pop("market_odds_stale", None)
        primary = found[0]
        forecast = _number(event.get("forecast"))
        # Forecast-relative odds only for strict "Above X" ladders in the same
        # unit as the forecast (e.g. CPI "0.3%" vs strike 0.3).
        if primary.get("kind") == "ladder" and forecast is not None:
            ladder_all = primary["thresholds_all"]
            exact = [t for t in ladder_all if abs(t["strike"] - forecast) < 1e-9] if primary.get("strict") else []
            below = [t for t in ladder_all if t["strike"] < forecast]
            above = [t for t in ladder_all if t["strike"] > forecast]
            if not exact and below and above:
                # Continuous series (NFP, claims): interpolate between strikes.
                lo, hi = below[-1], above[0]
                frac = (forecast - lo["strike"]) / (hi["strike"] - lo["strike"])
                p_higher = round(lo["prob"] + (hi["prob"] - lo["prob"]) * frac, 4)
                primary["p_above_forecast"] = p_higher
                primary["p_below_forecast"] = round(1 - p_higher, 4)
                primary["p_interpolated"] = True
                key = "higher" if p_higher >= 0.5 else "lower"
                primary["lean"] = {"scenario": key, "prob": max(p_higher, 1 - p_higher),
                                   "btc_direction": (event.get("scenarios") or {}).get(key, {}).get("btc_direction")}
            if exact:
                ladder = primary["thresholds_all"]
                idx = ladder.index(exact[0])
                p_higher = exact[0]["prob"]
                primary["p_above_forecast"] = p_higher
                if idx > 0:
                    # Discrete strikes: P(< forecast) = 1 - P(> previous strike).
                    p_lower = round(max(0.0, 1 - ladder[idx - 1]["prob"]), 4)
                    p_inline = round(max(0.0, 1 - p_higher - p_lower), 4)
                    primary["p_below_forecast"], primary["p_inline_forecast"] = p_lower, p_inline
                    choices = {"higher": p_higher, "inline": p_inline, "lower": p_lower}
                else:
                    choices = {"higher": p_higher, "inline_or_lower": round(1 - p_higher, 4)}
                key = max(choices, key=choices.get)
                primary["lean"] = {"scenario": key, "prob": choices[key],
                                   "btc_direction": "NEUTRAL" if key == "inline" else
                                   (event.get("scenarios") or {}).get(key, {}).get("btc_direction")}
        event["market_odds"] = found
    return enriched


def _carry_market_odds(events: List[Dict[str, Any]], old_events: List[Dict[str, Any]]) -> None:
    old_by_id = {e.get("event_id"): e.get("market_odds") for e in old_events if e.get("market_odds")}
    for event in events:
        if not event.get("market_odds") and old_by_id.get(event.get("event_id")):
            event["market_odds"] = copy.deepcopy(old_by_id[event["event_id"]])
            event["market_odds_stale"] = True


def market_odds_lines(event: Dict[str, Any]) -> List[str]:
    """Plain-text summary of prediction-market odds for Telegram."""
    pct = lambda p: f"{p * 100:.0f}%"
    lines: List[str] = []
    for odds in event.get("market_odds") or []:
        if odds.get("kind") == "outcomes":
            lines.append(f"{odds.get('question')}: " + " · ".join(
                f"{o['label']} {pct(o['prob'])}" for o in odds.get("outcomes", [])[:3]))
        else:
            parts = " · ".join(f"{t['label']}: {pct(t['prob'])}" for t in odds.get("thresholds", [])[:5])
            lines.append(f"{odds.get('question')}: {parts}")
            likely = odds.get("most_likely")
            if likely:
                lines.append(f"  Nhiều khả năng nhất: {likely['label']} ({pct(likely['prob'])})")
            if odds.get("p_above_forecast") is not None:
                lean = odds.get("lean") or {}
                text = f"  Cao hơn Forecast: {pct(odds['p_above_forecast'])}"
                if odds.get("p_inline_forecast") is not None:
                    text += f" · Đúng Forecast: {pct(odds['p_inline_forecast'])} · Thấp hơn: {pct(odds['p_below_forecast'])}"
                elif odds.get("p_below_forecast") is not None:
                    text += f" · Thấp hơn: {pct(odds['p_below_forecast'])} (ước tính nội suy)"
                if lean.get("btc_direction"):
                    text += f" → nghiêng BTC {lean['btc_direction']}"
                lines.append(text)
    if lines and event.get("market_odds_stale"):
        lines.append("(số liệu lần cập nhật trước)")
    return lines


SHORT_TITLES: Dict[str, str] = {
    "CPI": "CPI (lạm phát)", "CORE_CPI": "Core CPI", "PPI": "PPI (giá sản xuất)",
    "PCE": "PCE", "CORE_PCE": "Core PCE", "NFP": "NFP (việc làm)",
    "UNEMPLOYMENT": "Tỷ lệ thất nghiệp", "JOBLESS_CLAIMS": "Đơn trợ cấp thất nghiệp",
    "GDP": "GDP", "RETAIL_SALES": "Doanh số bán lẻ", "JOLTS": "JOLTS (việc làm trống)",
    "ISM_MANUFACTURING": "ISM sản xuất", "ISM_SERVICES": "ISM dịch vụ",
    "FOMC_RATE": "Fed quyết định lãi suất", "FED_PRESS_CONFERENCE": "Họp báo Fed",
}

# |P(up) - P(down)| below this is reported as "balanced", not a lean.
_BIAS_DEADBAND = 0.05


def _btc_reaction(event: Dict[str, Any], key: str) -> Optional[str]:
    """UP/DOWN typical initial BTC reaction when the print lands on ``key`` side."""
    direction = (((event.get("scenarios") or {}).get(key) or {}).get("btc_direction") or "").upper()
    return {"BULLISH": "UP", "BEARISH": "DOWN"}.get(direction)


def _ladder_split(thresholds: List[Dict[str, Any]], strict: bool,
                  ref: float) -> Optional[Tuple[float, float, float, bool]]:
    """Split a Kalshi threshold ladder around ``ref``.

    Returns ``(p_higher, p_inline, p_lower, approximate)``. An "inline" bucket
    exists only for 0.1-step percentage ladders (CPI, unemployment...), where
    the official print is rounded to the same grid as the strikes.
    """
    points = sorted((float(t["strike"]), float(t["prob"])) for t in thresholds
                    if t.get("strike") is not None and t.get("prob") is not None)
    if len(points) < 2:
        return None
    strikes = [s for s, _ in points]
    step = min(b - a for a, b in zip(strikes, strikes[1:]))
    exact = next((i for i, (s, _) in enumerate(points) if abs(s - ref) < 1e-9), None)
    if exact is not None and strict:
        p_hi = points[exact][1]
        if exact > 0 and step <= 0.1001:
            p_lo = max(0.0, 1.0 - points[exact - 1][1])
            p_in = max(0.0, 1.0 - p_hi - p_lo)
            return p_hi, p_in, p_lo, False
        if exact > 0 or step > 0.1001:
            return p_hi, 0.0, max(0.0, 1.0 - p_hi), False
    if ref <= strikes[0]:
        p_hi, approx = points[0][1], True
    elif ref >= strikes[-1]:
        p_hi, approx = points[-1][1], True
    else:
        lo = max(i for i, s in enumerate(strikes) if s <= ref)
        (s0, p0), (s1, p1) = points[lo], points[lo + 1]
        p_hi = p0 + (p1 - p0) * (ref - s0) / (s1 - s0)
        approx = abs(s0 - ref) > 1e-9
    return p_hi, 0.0, max(0.0, 1.0 - p_hi), approx


def _lean(up: float, down: float, flat: float) -> str:
    if flat > max(up, down):
        return "FLAT"
    if abs(up - down) < _BIAS_DEADBAND:
        return "BALANCED"
    return "UP" if up > down else "DOWN"


def compute_btc_bias(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Probability BTC rises/falls on release: Kalshi odds × typical BTC reaction.

    Data releases compare the Kalshi ladder with the Forex Factory forecast.
    FOMC maps cut → BTC up, hike → BTC down, hold → flat. The reaction mapping is
    the usual initial move, never a guarantee.
    """
    if event.get("category") == "FED_PRESS_CONFERENCE":
        return None
    odds = event.get("market_odds") or []
    primary = odds[0] if odds else None
    rule = {"higher": _btc_reaction(event, "higher"), "lower": _btc_reaction(event, "lower")}
    if not primary:
        return {"available": False, "reason": "Chưa có tỷ lệ cược Kalshi", "rule": rule}
    result: Dict[str, Any] = {"available": True, "source": "Kalshi", "rule": rule,
                              "stale": bool(event.get("market_odds_stale"))}
    if primary.get("kind") == "outcomes":
        outcomes = primary.get("outcomes") or []
        total = sum(float(o.get("prob") or 0) for o in outcomes)
        if total <= 0:
            return {"available": False, "reason": "Tỷ lệ cược trống", "rule": rule}
        up = sum(float(o["prob"]) for o in outcomes if re.search(r"\bcut\b", str(o.get("label")), re.I)) / total
        down = sum(float(o["prob"]) for o in outcomes if re.search(r"\bhike\b", str(o.get("label")), re.I)) / total
        flat = max(0.0, 1.0 - up - down)
        result.update({"basis": "outcomes", "outcomes": outcomes[:4]})
    else:
        ref = _number(event.get("forecast"))
        if ref is None:
            return {"available": False, "rule": rule,
                    "reason": "Chờ số dự báo (thường có trong tuần ra tin)"}
        if not rule["higher"] or not rule["lower"] or rule["higher"] == rule["lower"]:
            return {"available": False, "reason": "Không có hướng phản ứng rõ", "rule": rule}
        split = _ladder_split(primary.get("thresholds_all") or [], bool(primary.get("strict")), ref)
        if not split:
            return {"available": False, "reason": "Tỷ lệ cược chưa đủ thanh khoản", "rule": rule}
        p_hi, p_in, p_lo, approx = split
        up = p_hi if rule["higher"] == "UP" else p_lo
        down = p_lo if rule["higher"] == "UP" else p_hi
        flat = p_in
        result.update({"basis": "forecast", "reference": event.get("forecast"),
                       "p_higher": round(p_hi, 4), "p_inline": round(p_in, 4),
                       "p_lower": round(p_lo, 4), "approx": approx})
    up, down, flat = round(up, 4), round(down, 4), round(flat, 4)
    result.update({"up": up, "down": down, "flat": flat, "lean": _lean(up, down, flat)})
    return result


def attach_btc_bias(events: List[Dict[str, Any]]) -> None:
    for event in events:
        category = str(event.get("category") or "")
        event["short_title"] = SHORT_TITLES.get(category, event.get("title"))
        # Re-derive so cached events pick up the current reaction rules.
        event["scenarios"] = scenarios_for(category, str(event.get("title") or ""))
        try:
            event["btc_bias"] = compute_btc_bias(event)
        except Exception as exc:  # never let one malformed ladder break the refresh
            logger.warning("[MacroCalendar] BTC bias failed for %s: %s", event.get("category"), exc)
            event["btc_bias"] = None


def display_percentages(bias: Dict[str, Any]) -> Dict[str, int]:
    """Round DOWN/FLAT/UP with largest remainders so display totals exactly 100."""
    keys = ("down", "flat", "up")
    values = [max(0.0, float(bias.get(key, 0) or 0)) for key in keys]
    total = sum(values)
    if total <= 0:
        return {key: 0 for key in keys}
    raw = [value / total * 100.0 for value in values]
    rounded = [int(value) for value in raw]
    remaining = 100 - sum(rounded)
    order = sorted(range(len(keys)), key=lambda index: raw[index] - rounded[index], reverse=True)
    for index in order[:remaining]:
        rounded[index] += 1
    return dict(zip(keys, rounded))


def group_btc_bias(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Impact-weighted average across indicators released at the same time."""
    total_w = up = down = flat = 0.0
    for event in events:
        bias = event.get("btc_bias") or {}
        if not bias.get("available"):
            continue
        weight = 2.0 if event.get("impact") == "HIGH" else 1.0
        total_w += weight
        up += weight * bias["up"]
        down += weight * bias["down"]
        flat += weight * bias["flat"]
    if total_w <= 0:
        return None
    up, down, flat = round(up / total_w, 4), round(down / total_w, 4), round(flat / total_w, 4)
    return {"up": up, "down": down, "flat": flat, "lean": _lean(up, down, flat)}


_ENRICH_KEYS = ("forecast", "previous", "actual", "unit", "consensus_label", "consensus_extra",
                "enrichment_provider", "enrichment_source_url", "enrichment_date",
                "enrichment_event", "enrichment_importance", "surprise", "surprise_direction")


def _carry_enrichment(events: List[Dict[str, Any]], old_events: List[Dict[str, Any]]) -> None:
    """Keep consensus fetched earlier (feed only covers the current week)."""
    old_by_id = {e.get("event_id"): e for e in old_events if e.get("enrichment_provider")}
    for event in events:
        old = old_by_id.get(event.get("event_id"))
        if old and not event.get("enrichment_provider"):
            for key in _ENRICH_KEYS:
                if key in old:
                    event[key] = copy.deepcopy(old[key])


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
        self.reminder_hours = max(0, float(getattr(config, "MACRO_CALENDAR_REMINDER_HOURS", 1)))
        self.odds_refresh_seconds = max(300, int(getattr(config, "MACRO_CALENDAR_ODDS_REFRESH_SECONDS", 900)))
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
        self._ff_payload: Optional[List[Any]] = None
        self._ff_fetched_mono: Optional[float] = None
        self._odds_refreshed_mono: Optional[float] = None
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
            elif (self._odds_refreshed_mono is None
                  or time.monotonic() - self._odds_refreshed_mono >= self.odds_refresh_seconds):
                try:
                    self.refresh_odds()
                except Exception:
                    logger.exception("[MacroCalendar] Odds refresh failed")
                finally:
                    self._odds_refreshed_mono = time.monotonic()
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

    def _apply_forexfactory(self, events: List[Dict[str, Any]], errors: Dict[str, str]) -> List[Dict[str, Any]]:
        """Merge FF forecast/previous. The feed returns HTTP 429 when polled
        often, so it is fetched at most every 30 min and the payload reused."""
        ff_payload = self._ff_payload
        if self._ff_fetched_mono is None or time.monotonic() - self._ff_fetched_mono >= 1800:
            self._ff_fetched_mono = time.monotonic()
            try:
                ff_payload = json.loads(self._get_text(FF_CALENDAR_URL))
                self._ff_payload = ff_payload
            except Exception as exc:
                errors["Forex Factory"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[MacroCalendar] Forex Factory consensus failed: %s", exc)
        if ff_payload is None:
            return events
        try:
            return enrich_forexfactory(events, ff_payload)
        except Exception as exc:
            errors["Forex Factory"] = f"{type(exc).__name__}: {exc}"
            return events

    def _apply_kalshi(self, events: List[Dict[str, Any]], errors: Dict[str, str]) -> List[Dict[str, Any]]:
        payloads: Dict[str, Any] = {}
        failures: List[str] = []
        for series in sorted({s for values in KALSHI_SERIES.values() for s in values}):
            try:
                response = self._http_get(
                    KALSHI_EVENTS_URL,
                    params={"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 6},
                    timeout=15, headers={"User-Agent": _USER_AGENT},
                )
                response.raise_for_status()
                payloads[series] = response.json()
            except Exception as exc:
                failures.append(f"{series}: {type(exc).__name__}: {exc}")
        if failures:
            errors["Kalshi"] = "; ".join(failures)[:500]
            logger.warning("[MacroCalendar] Kalshi odds partial failure: %s", errors["Kalshi"])
        try:
            return enrich_kalshi(events, payloads)
        except Exception as exc:
            errors["Kalshi"] = f"{type(exc).__name__}: {exc}"
            return events

    def _stamp_status(self, events: List[Dict[str, Any]], now: datetime) -> None:
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

    def refresh_odds(self) -> None:
        """Light refresh: re-price Kalshi odds/FF forecasts on cached events.

        Runs on the same worker thread as :meth:`refresh_now`, so the two never
        overlap; reminders always read odds at most ``odds_refresh_seconds`` old.
        """
        with self._lock:
            if self._refreshing or not self._data.get("events"):
                return
            old_events = copy.deepcopy(self._data["events"])
            self._refreshing = True
        errors: Dict[str, str] = {}
        try:
            events = self._apply_forexfactory(copy.deepcopy(old_events), errors)
            events = self._apply_kalshi(events, errors)
            _carry_market_odds(events, old_events)
            attach_btc_bias(events)
            self._stamp_status(events, self._now())
            self._odds_refreshed_mono = time.monotonic()
            with self._lock:
                source_errors = dict(self._data.get("source_errors") or {})
                for name in ("Forex Factory", "Kalshi"):
                    source_errors.pop(name, None)
                source_errors.update(errors)
                self._data["events"] = events
                self._data["source_errors"] = source_errors
                self._data["odds_refreshed_at"] = _iso(self._now())
                self._persist_locked()
        finally:
            with self._lock:
                self._refreshing = False

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

            events = self._apply_forexfactory(events, errors)
            _carry_enrichment(events, old_events)

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

            events = self._apply_kalshi(events, errors)
            _carry_market_odds(events, old_events)
            attach_btc_bias(events)
            self._odds_refreshed_mono = time.monotonic()

            self._stamp_status(events, now)

            any_success = any(successful.values())
            with self._lock:
                # A reminder may have been claimed/sent while network adapters
                # were running; merge the current ledger rather than the stale
                # refresh-start copy.
                current_reminders = copy.deepcopy(self._data.get("reminders", {}))
                self._data = {
                    "schema_version": SCHEMA_VERSION, "events": events, "reminders": current_reminders,
                    "fetched_at": _iso(now), "odds_refreshed_at": _iso(now),
                    "last_success_at": _iso(now) if any_success else self._data.get("last_success_at"),
                    "source_errors": errors, "refreshing": False,
                    "error": None if any_success else "Không thể cập nhật nguồn; đang giữ dữ liệu tốt gần nhất",
                }
                self._persist_locked()
        finally:
            with self._lock:
                self._refreshing = False
        return self.snapshot()

    # ── Telegram reminders ─────────────────────────────────────────────
    def _reminder_groups(self, events: List[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
        """Group indicators released at the same instant into one message."""
        order = {cat: i for i, cat in enumerate(SHORT_TITLES)}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        press: Dict[str, Dict[str, Any]] = {}
        for event in events:
            when = event.get("scheduled_at_utc")
            if not _parse_iso(when):
                continue
            if event.get("category") == "FED_PRESS_CONFERENCE":
                press[when] = event
                continue
            groups.setdefault(when, []).append(event)
        for when, group in groups.items():
            seen: set = set()
            unique = []
            for event in group:
                if event.get("category") in seen:
                    continue
                seen.add(event.get("category"))
                unique.append(event)
            groups[when] = sorted(unique, key=lambda e: (e.get("impact") != "HIGH",
                                                         order.get(e.get("category"), 99)))
        return groups, press

    def process_reminders(self) -> int:
        if not self.enabled or not self.reminder_enabled:
            return 0
        now = self._now()
        with self._lock:
            events = copy.deepcopy(self._data.get("events", []))
        groups, press = self._reminder_groups(events)
        sent_count = 0
        for when, group in sorted(groups.items()):
            if self.reminder_only_high and not any(e.get("impact") == "HIGH" for e in group):
                continue
            scheduled = _parse_iso(when)
            target = scheduled - timedelta(hours=self.reminder_hours)
            if not (target <= now < min(target + timedelta(seconds=self.reminder_window), scheduled)):
                continue
            key = f"group|{_iso(scheduled)}|{self.reminder_hours:g}h"
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
                    "categories": [e.get("category") for e in group],
                }
                self._persist_locked()  # durable write-ahead claim
            press_event = None
            if any(e.get("category") == "FOMC_RATE" for e in group):
                press_event = next((p for p_when, p in press.items()
                                    if 0 <= (_parse_iso(p_when) - scheduled).total_seconds() <= 7200), None)
            message_id = 0
            try:
                message_id = int(self.notifier.telegram.send(
                    self._reminder_message(group, scheduled, now, press_event)) or 0)
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

    def _reminder_message(self, group: List[Dict[str, Any]], scheduled: datetime, now: datetime,
                          press_event: Optional[Dict[str, Any]] = None) -> str:
        """Short Telegram message: will BTC rise or fall when this prints?"""
        esc = html.escape
        local = scheduled.astimezone(self._vn_tz) if self._vn_tz else scheduled
        weekday = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")[local.weekday()]
        minutes = max(1, round((scheduled - now).total_seconds() / 60))

        def pct(p: float) -> str:
            return f"{p * 100:.0f}%"

        def split(bias: Dict[str, Any]) -> str:
            shown = display_percentages(bias)
            return (f"🔴 BTC giảm <b>{shown['down']}%</b> │ 🟢 BTC tăng <b>{shown['up']}%</b>"
                    f" │ ⚪ Đi ngang {shown['flat']}%")

        def value(event: Dict[str, Any], name: str) -> Optional[str]:
            raw = event.get(name)
            if raw is None or raw == "":
                return None
            unit, text = str(event.get("unit") or ""), str(raw)
            return text if not unit or text.strip().endswith(unit) else f"{text}{unit}"

        reaction = {"UP": "🟢 BTC tăng", "DOWN": "🔴 BTC giảm"}

        def rule_line(event: Dict[str, Any]) -> str:
            if event.get("category") == "FOMC_RATE":
                return "↳ Fed tăng lãi suất → 🔴 BTC giảm · Fed hạ → 🟢 BTC tăng"
            hi, lo = _btc_reaction(event, "higher"), _btc_reaction(event, "lower")
            if not hi or not lo:
                return "↳ Phản ứng BTC phụ thuộc bối cảnh"
            return f"↳ Ra cao hơn dự báo → {reaction[hi]} · thấp hơn → {reaction[lo]}"

        overall = group_btc_bias(group)
        lines = [
            f"⏰ <b>CÒN {minutes} PHÚT RA TIN VĨ MÔ MỸ</b>",
            f"🇻🇳 <b>{local.strftime('%H:%M')} · {weekday} {local.strftime('%d/%m/%Y')}</b>",
            "",
        ]
        if overall:
            icon, label = _LEAN_TEXT[overall["lean"]]
            scope = f"TỔNG HỢP {len(group)} CHỈ SỐ" if len(group) > 1 else "CHỈ SỐ NÀY"
            lines += [f"{icon} <b>{scope}: {label}</b>", split(overall)]
        else:
            lines.append("🟡 <b>Chưa có tỷ lệ cược</b> — xem quy tắc từng chỉ số bên dưới")

        stale = False
        for event in group:
            bias = event.get("btc_bias") or {}
            icon = _LEAN_TEXT[bias["lean"]][0] if bias.get("available") else "▫️"
            title = esc(str(event.get("short_title") or SHORT_TITLES.get(event.get("category"), event.get("title") or "")))
            badge = " · <i>HIGH</i>" if event.get("impact") == "HIGH" else ""
            lines += ["", f"{icon} <b>TÁC ĐỘNG RIÊNG — {title}</b>{badge}"]
            if bias.get("basis") == "outcomes":
                names = []
                for outcome in bias.get("outcomes") or []:
                    if float(outcome.get("prob") or 0) < 0.01:
                        continue
                    label = (str(outcome.get("label") or "")
                             .replace("Fed maintains rate", "Fed giữ nguyên")
                             .replace("Hike", "Fed tăng").replace("Cut", "Fed hạ"))
                    names.append(f"{esc(label)} {pct(float(outcome['prob']))}")
                if names:
                    lines.append("Kalshi: " + " · ".join(names[:3]))
            else:
                forecast, previous = value(event, "forecast"), value(event, "previous")
                if forecast or previous:
                    lines.append(f"Dự báo <b>{esc(forecast or 'chưa có')}</b> · Kỳ trước {esc(previous or 'chưa có')}")
            if bias.get("available"):
                lines.append(split(bias))
                stale = stale or bool(bias.get("stale"))
            elif bias.get("reason"):
                lines.append(f"<i>{esc(str(bias['reason']))}</i>")
            lines.append(rule_line(event))
        if press_event:
            press_at = _parse_iso(press_event.get("scheduled_at_utc"))
            if press_at:
                press_local = press_at.astimezone(self._vn_tz) if self._vn_tz else press_at
                lines += ["", f"🎙 Họp báo Fed lúc <b>{press_local.strftime('%H:%M')}</b> — biến động có thể tiếp diễn"]
        lines += ["", "<i>Tỷ lệ = giá cược Kalshi so với dự báo Forex Factory"
                  + (" (lần cập nhật trước)" if stale else "")
                  + ". Hướng BTC là phản ứng thường gặp, không chắc chắn.</i>"]
        return "\n".join(lines)


_LEAN_TEXT: Dict[str, Tuple[str, str]] = {
    "UP": ("🟢", "BTC DỰ KIẾN TĂNG"),
    "DOWN": ("🔴", "BTC DỰ KIẾN GIẢM"),
    "FLAT": ("⚪", "BTC DỰ KIẾN ĐI NGANG"),
    "BALANCED": ("🟡", "BTC CHƯA RÕ HƯỚNG"),
}
