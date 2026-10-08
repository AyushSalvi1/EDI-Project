"""
Import college data from a PDF instead of JSON.

Real college PDFs are inconsistent: column order changes, headers are phrased
differently, dates appear as "05 Jan 2026" or "2026-01-05", lab subjects are
marked "Lab", "Practical" or "P", and most documents contain only a single
teacher/subject/division table with no separate subject or room table at all.

This module therefore does not require an exact template. It extracts every
table from the PDF, normalises the headers, guesses what each table is and how
its columns map onto the canonical fields, and reports a confidence score so an
administrator can correct the guess before anything is written. The result is
handed to the existing importer, so PDF and JSON imports share one validation
path and one atomic write.
"""

import re
from collections import defaultdict
from datetime import datetime

import pdfplumber

from .models import year_prefix_for

MAX_TABLES = 60
MAX_PREVIEW_ROWS = 4000

ROLE_CALENDAR = "calendar"
ROLE_SUBJECTS = "subjects"
ROLE_ROOMS = "rooms"
ROLE_ALLOCATIONS = "allocations"

# Canonical fields per table role, with the header phrases that identify them.
ROLE_FIELDS = {
    ROLE_CALENDAR: {
        "college_name": ["college name", "institution", "college", "institute", "university", "school"],
        "semester_name": ["semester name", "semester", "term", "session", "academic term"],
        "start_date": ["start date", "commencing", "commencement", "from date", "date from",
                       "session start", "start"],
        "end_date": ["end date", "to date", "date to", "session end", "ends on", "end"],
        "working_days": ["working days", "working day", "week days", "weekday", "days", "class days"],
        "daily_start_time": ["daily start time", "start time", "college start", "timing start",
                             "classes start", "day start"],
        "daily_end_time": ["daily end time", "end time", "college end", "timing end",
                           "classes end", "day end"],
        "period_duration_minutes": ["period duration", "period duration minutes", "period length",
                                    "duration", "minutes per period", "period minutes"],
        "lunch_start": ["lunch start", "lunch break start", "lunch from", "break start"],
        "lunch_end": ["lunch end", "lunch break end", "lunch to", "break end"],
    },
    ROLE_SUBJECTS: {
        "subject_id": ["subject code", "subject id", "course code", "code", "subject no",
                       "paper code", "subject number"],
        "subject_name": ["subject name", "subject", "course name", "course", "paper name",
                         "subject title", "name"],
        "is_lab": ["type", "is lab", "lab", "category", "kind", "nature", "subject type",
                   "class type"],
    },
    ROLE_ROOMS: {
        "room_name": ["room name", "room", "classroom", "hall", "venue", "lab name", "room no"],
        "is_lab": ["type", "is lab", "lab", "category", "kind", "room type", "nature"],
        "capacity": ["capacity", "seats", "seat capacity", "strength", "size", "room capacity",
                     "students"],
    },
    ROLE_ALLOCATIONS: {
        "teacher": ["teacher", "faculty", "teacher name", "faculty name", "professor", "prof",
                    "instructor", "lecturer", "staff", "faculty member", "handled by"],
        "max_hours": ["max hours", "max hours per week", "maximum hours", "weekly limit",
                      "max hours/week", "contract hours", "load limit", "max load",
                      "max hrs", "max hrs week", "hrs per week", "weekly max", "max load hrs"],
         "year": ["year", "yr", "year of study", "standard", "semester year", "class"],
        "division": ["division", "div", "division no", "batch", "section", "division number",
                     "class division"],
        "subject_id": ["subject code", "subject id", "course code", "code", "paper code",
                       "subject no", "subject number"],
        "subject_name": ["subject name", "subject", "course name", "course", "paper name",
                         "subject title"],
        "total_hours": ["total hours", "hours", "semester hours", "total hours for semester",
                        "hrs", "hours per semester", "total", "credits", "contact hours"],
        "unavailable_slots": ["unavailable", "unavailable slots", "unavailability",
                              "not available", "unavailable periods"],
        "strength": ["strength", "division strength", "students", "seats", "capacity",
                     "division size"],
        "is_lab": ["type", "is lab", "lab", "category", "kind", "nature", "subject type",
                   "class type"],
    },
}

# Tables below this mapping confidence are ignored rather than guessed at, because
# a badly identified table silently injects bogus rooms or subjects.
MIN_CONFIDENCE = {
    ROLE_CALENDAR: 20,
    ROLE_SUBJECTS: 40,
    ROLE_ROOMS: 40,
    # Teacher, subject, year, division and hours are the five columns an
    # allocation table cannot import without, and mapping exactly those five
    # scores 50%. Optional columns such as strength or unavailability push it
    # higher, so 50 accepts the minimum viable table.
    ROLE_ALLOCATIONS: 50,
}

# Header phrases that strongly identify which kind of table this is.
ROLE_SIGNATURES = {
    ROLE_ALLOCATIONS: ["teacher", "faculty", "subject", "division", "hours"],
    ROLE_ROOMS: ["room", "classroom", "hall", "lab", "capacity", "seats"],
    ROLE_SUBJECTS: ["subject", "code", "course", "lab", "theory"],
    ROLE_CALENDAR: ["semester", "start date", "end date", "working days", "college"],
}

LAB_WORDS = {"lab", "laboratory", "practical", "lab.", "practical.", "lab based",
             "practical based", "tutorial", "workshop", "studio", "p"}


# ===========================================================================
# Text / value parsing helpers
# ===========================================================================
def normalize_header(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    value = str(text or "").strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_cell(value):
    """Normalise a table cell: strip newlines, collapse internal whitespace."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text


def parse_int(value, default=None):
    """Extract an integer from messy cells such as '45 hrs', '60', '3.0'."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = clean_cell(value)
    if not text:
        return default
    match = re.search(r"-?\d+(?:[.,]\d+)?", text.replace(",", ""))
    if not match:
        return default
    number = float(match.group(0))
    return int(round(number))


def parse_bool(value, default=False):
    """Interpret a lab/theory marker."""
    if isinstance(value, bool):
        return value
    text = normalize_header(value)
    if not text:
        return default
    if text in {"yes", "y", "true", "1", "lab", "laboratory", "practical", "tutorial"}:
        return True
    if text in {"no", "n", "false", "0", "theory", "lecture", "non lab", "nonlab"}:
        return False
    if any(word in text for word in ("lab", "practical")):
        return True
    return default


DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
    "%b %d %Y", "%B %d %Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y",
)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(value):
    """Accept ISO, DD/MM/YYYY, '5 Jan 2026', 'Jan 5, 2026' and similar."""
    text = clean_cell(value)
    if not text:
        return None
    text = text.replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.title(), fmt).date()
        except ValueError:
            continue

    match = re.search(r"(\d{1,2})\s*[-/ ]\s*([A-Za-z]{3,9})\s*[-/ ]?\s*(\d{2,4})", text)
    if match:
        day, month_name, year = match.groups()
        month = MONTHS.get(normalize_header(month_name)[:4]) or MONTHS.get(normalize_header(month_name)[:3])
        if month:
            year = int(year)
            if year < 100:
                year += 2000
            try:
                return datetime(year, month, int(day)).date()
            except ValueError:
                return None

    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        except ValueError:
            return None
    return None


def parse_time(value):
    """
    Parse the first time found in a cell. Accepts '09:00', '9:00 AM', '09.00',
    '0900'. For a range such as '09:00 am to 05:00 pm' use parse_time_range.
    """
    text = clean_cell(value)
    if not text:
        return None

    match = re.search(
        r"(\d{1,2})\s*[:.]\s*(\d{2})\s*([ap])\.?\s*m\.?", text, re.IGNORECASE
    )
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = match.group(3).lower()
        if meridiem == "p" and hour < 12:
            hour += 12
        if meridiem == "a" and hour == 12:
            hour = 0
        return _valid_time(hour, minute)

    match = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})", text)
    if match:
        return _valid_time(int(match.group(1)), int(match.group(2)))

    match = re.fullmatch(r"\s*(\d{3,4})\s*", text)
    if match:
        digits = match.group(1).zfill(4)
        return _valid_time(int(digits[:2]), int(digits[2:]))

    return None


def parse_time_range(value):
    """
    Parse a cell containing two times, e.g. '09:00 am to 05:00 pm' or '09:00 - 17:00'.
    Returns (start, end) where either element may be None.
    """
    text = clean_cell(value)
    if not text:
        return None, None

    times = []
    for match in re.finditer(
        r"(\d{1,2})\s*[:.]\s*(\d{2})\s*([ap])\.?\s*m\.?", text, re.IGNORECASE
    ):
        parsed = parse_time(f"{match.group(1)}:{match.group(2)} {match.group(3)}m")
        if parsed:
            times.append(parsed)

    if not times:
        # Bare hours such as "9 am to 5 pm".
        for match in re.finditer(r"(\d{1,2})\s*([ap])\.?\s*m\.?", text, re.IGNORECASE):
            parsed = parse_time(f"{match.group(1)}:00 {match.group(2)}m")
            if parsed:
                times.append(parsed)

    if not times:
        single = parse_time(text)
        return (single, None) if single else (None, None)
    if len(times) == 1:
        return times[0], None
    return times[0], times[1]


def _valid_time(hour, minute):
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


DAY_TOKENS = {
    "mon": "MON", "monday": "MON", "tue": "TUE", "tues": "TUE", "tuesday": "TUE",
    "wed": "WED", "weds": "WED", "wednesday": "WED", "thu": "THU", "thur": "THU",
    "thurs": "THU", "thursday": "THU", "fri": "FRI", "friday": "FRI",
    "sat": "SAT", "saturday": "SAT",
}
DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]


def parse_days(value):
    """Accept 'MON,TUE', 'Mon Tue Wed', 'Monday to Friday', or a JSON list."""
    if isinstance(value, (list, tuple)):
        tokens = [str(v) for v in value]
    else:
        text = clean_cell(value)
        if not text:
            return []
        text = re.sub(r"[,;/|]", " ", text)
        text = re.sub(r"(?<=\w)\s*-\s*(?=\w)", " ", text)
        tokens = text.split()

    found = set()
    for token in tokens:
        key = normalize_header(token)
        if key in DAY_TOKENS:
            found.add(DAY_TOKENS[key])

    # "Monday to Friday" -> expand the range.
    if len(tokens) >= 3:
        keys = [normalize_header(t) for t in tokens]
        for i in range(len(keys) - 2):
            if keys[i] in DAY_TOKENS and keys[i + 1] in {"to", "till", "through"} and keys[i + 2] in DAY_TOKENS:
                start = DAY_ORDER.index(DAY_TOKENS[keys[i]])
                end = DAY_ORDER.index(DAY_TOKENS[keys[i + 2]])
                if start <= end:
                    found.update(DAY_ORDER[start:end + 1])

    return [d for d in DAY_ORDER if d in found]


def parse_slots(value):
    """
    Accept 'MON-P1, FRI-P4', 'Mon 1', 'Monday Period 2', 'MON P 3'.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = [p for p in re.split(r"[,;/|]", clean_cell(value)) if p.strip()]

    slots = []
    for part in parts:
        text = clean_cell(part)
        match = re.search(r"(mon|tue|wed|thu|thurs|fri|sat)[a-z]*\s*[-–]?\s*p?\s*(\d{1,2})",
                          text, re.IGNORECASE)
        if match:
            day = normalize_header(match.group(1))
            day_code = DAY_TOKENS.get(day)
            period = int(match.group(2))
            if day_code and period > 0:
                slots.append(f"{day_code}-P{period}")
    return sorted(set(slots))


def slugify_subject(name, fallback):
    """Build a stable subject code from a subject name."""
    code = re.sub(r"[^A-Za-z0-9]+", "", str(name or "")).upper()
    if not code:
        code = fallback
    return code[:24]


# ===========================================================================
# Header matching
# ===========================================================================
def _score_header(normalized, alias):
    if not normalized or not alias:
        return 0
    if normalized == alias:
        return 100
    if alias in normalized:
        # Prefer the tightest substring match.
        return 80 - min(20, len(normalized) - len(alias))
    header_tokens = set(normalized.split())
    alias_tokens = set(alias.split())
    if not alias_tokens:
        return 0
    if alias_tokens.issubset(header_tokens):
        return 70
    overlap = header_tokens & alias_tokens
    if overlap:
        return int(40 * len(overlap) / len(alias_tokens))
    return 0


def guess_mapping(headers, role):
    """
    For each canonical field of ``role`` pick the best-matching, not-yet-used
    column index. Returns (mapping, confidence, unmapped_headers).
    """
    fields = ROLE_FIELDS[role]
    normalized = [normalize_header(h) for h in headers]
    mapping = {}
    used = set()

    for field, aliases in fields.items():
        best_index, best_score = None, 0
        for index, header in enumerate(normalized):
            if index in used:
                continue
            score = max(_score_header(header, alias) for alias in aliases)
            if score > best_score:
                best_index, best_score = index, score
        if best_index is not None and best_score >= 40:
            mapping[field] = best_index
            used.add(best_index)

    matched = len(mapping)
    total = len(fields)
    confidence = int(100 * matched / total) if total else 0
    unmapped = [
        normalize_header(headers[i]) for i in range(len(headers)) if i not in used
    ]
    return mapping, confidence, unmapped


def classify_table(headers):
    """Guess which canonical role a table plays, with a confidence score."""
    normalized = [normalize_header(h) for h in headers]
    blob = " ".join(normalized)
    scores = {}
    for role, phrases in ROLE_SIGNATURES.items():
        score = 0
        for phrase in phrases:
            if any(phrase in header for header in normalized):
                score += 20
            elif phrase in blob:
                score += 8
        scores[role] = score
    role = max(scores, key=lambda r: scores[r])
    return role, scores[role]


# ===========================================================================
# Table extraction
# ===========================================================================
CALENDAR_KEY_HINTS = (
    "semester", "session", "term", "date", "commenc", "conclude", "working", "week day",
    "weekday", "timing", "college", "institution", "lunch", "break", "duration", "hours",
    "start", "end", "from", "to",
)


def _numeric_count(row):
    return sum(1 for cell in row if re.fullmatch(r"-?\d+(?:[.,]\d+)?", clean_cell(cell).replace(",", "")))


def _header_affinity(row):
    """
    How strongly a row looks like a header rather than data.

    Counts how many cells match a known canonical header alias. A genuine
    header scores high; a spilled data row such as
    'Prof. Ravi Menon | Networks Lab | 4 | 1 | 30' scores low because most of
    its cells are names and numbers.
    """
    if not row:
        return 0
    aliases = []
    for fields in ROLE_FIELDS.values():
        aliases.extend(fields)
    score = 0
    for cell in row:
        normalized = normalize_header(cell)
        if not normalized:
            continue
        if re.fullmatch(r"-?\d+(?:[.,]\d+)?", normalized):
            continue
        best = 0
        for alias in aliases:
            if normalized == alias or alias in normalized:
                best = max(best, len(alias))
        if best >= 3:
            score += 1
    return score


def _looks_like_label_value_table(headers, rows):
    """
    Detect a two-column 'Label | Value' table, which is how most college PDFs
    present academic-calendar information. These have no real header row, so
    pdfplumber reports the first data pair as the header.
    """
    if len(headers) != 2 or len(rows) < 2:
        return False
    for row in [headers] + rows[:4]:
        key = normalize_header(row[0] if row else "")
        if not key:
            continue
        if any(hint in key for hint in CALENDAR_KEY_HINTS):
            return True
    return False


def _merge_continuation_tables(raw_tables):
    """
    Join tables that are one logical table split across page breaks.

    A 30-division college produces hundreds of allocation rows, so the table
    always spills onto the next page and pdfplumber reports the continuation as
    a separate table whose first row is data, not a header. Such tables have the
    same width as the previous one and a first row containing several numbers.
    """
    merged = []
    for page_number, table in raw_tables:
        if not table:
            continue
        if merged:
            prev_page, prev_table = merged[-1]
            prev_width = max((len(r) for r in prev_table if r), default=0)
            cur_width = max((len(r) for r in table if r), default=0)
            first_row = table[0] if table else []
            same_width = prev_width == cur_width
            # Only allocation grids reliably spill across pages, so requiring the
            # previous table to be allocation-shaped avoids swallowing an
            # unrelated table that merely happens to share the same column count.
            prev_role, _prev_score = classify_table(prev_table[0] if prev_table else [])
            continuation = (
                page_number >= prev_page
                and same_width
                and len(table) > 1
                and prev_role == ROLE_ALLOCATIONS
                and _numeric_count(first_row) >= 2
                and _header_affinity(prev_table[0]) >= _header_affinity(first_row)
            )
            if continuation:
                merged[-1] = (prev_page, prev_table + [first_row] + list(table[1:]))
                continue
        merged.append((page_number, table))
    return merged


def classify_tables(raw_tables):
    """
    Turn raw {page, headers, rows} tables into classified, mapped tables.

    Columns are matched by header wording rather than position, so a PDF is
    imported correctly even when the source document reorders its columns.
    Tables already split across page breaks are rejoined first. This is
    separate from PDF reading so the mapping-review screen can re-classify a
    table after the administrator corrects its role.
    """
    # Accept either (page_number, raw_table) tuples from the reader or already
    # split dicts, which is what the review form round-trips through JSON.
    prepared = []
    for item in raw_tables:
        explicit_target = item.get("continues_table") if isinstance(item, dict) else None
        if isinstance(item, tuple):
            page_number, rows = item
            if not rows or len(rows) < 2:
                continue
            header_row = [clean_cell(c) for c in rows[0]]
            body = [[clean_cell(c) for c in row] for row in rows[1:] if row]
        else:
            page_number = item.get("page", 1)
            header_row = [clean_cell(c) for c in item.get("headers") or []]
            body = [[clean_cell(c) for c in row] for row in (item.get("rows") or [])]
        if not header_row or not body:
            continue

        # A continuation explicitly marked by the caller is appended to the
        # table it continues, and its header row is discarded.
        if explicit_target is not None:
            target = min(int(explicit_target), len(prepared) - 1)
            if target >= 0:
                width = len(prepared[target]["header_row"])
                for row in body:
                    prepared[target]["body"].append((row + [""] * width)[:width])
                continue

        prepared.append({"page": page_number, "header_row": header_row, "body": body})

    # Rejoin tables the reader split across a page break, detected by shape.
    flattened = [
        (entry["page"], [entry["header_row"]] + entry["body"]) for entry in prepared
    ]
    flattened = _merge_continuation_tables(flattened)

    results = []
    for table_index, (page_number, table) in enumerate(flattened[:MAX_TABLES]):
        if not table or len(table) < 2:
            continue
        header_row = [clean_cell(c) for c in table[0]]
        if not any(header_row):
            continue

        # Keep columns that have at least one value somewhere in the body.
        body = [[clean_cell(c) for c in row] for row in table[1:] if row]
        if not body:
            continue
        width = max([len(header_row)] + [len(r) for r in body])
        header_row = (header_row + [""] * width)[:width]
        body = [(r + [""] * width)[:width] for r in body]

        keep = [i for i in range(width) if any(r[i] for r in body)]
        if len(keep) < 2:
            continue
        header_row = [header_row[i] for i in keep]
        body = [[row[i] for i in keep] for row in body][:MAX_PREVIEW_ROWS]

        role, _raw_score = classify_table(header_row)
        mapping, confidence, unmapped = guess_mapping(header_row, role)

        # A two-column 'Label | Value' table is a calendar block even though its
        # first pair was consumed as the header row.
        if _looks_like_label_value_table(header_row, body):
            role = ROLE_CALENDAR
            confidence = 95

        if confidence < 40:
            # Only prefer a different role when one is a clear winner, so a
            # genuinely unrecognised table is reported as unknown rather than
            # being forced into the nearest table shape.
            best_role, best_result = max(
                ((r, guess_mapping(header_row, r)) for r in ROLE_FIELDS if r != role),
                key=lambda pair: pair[1][1],
            )
            if best_result[1] >= 40 and best_result[1] > confidence:
                role, (mapping, confidence, unmapped) = best_role, best_result
            elif confidence < 40:
                role = "unknown"
                mapping, confidence, unmapped = {}, 0, list(header_row)

        results.append({
            "index": table_index,
            "page": page_number,
            "role": role,
            "headers": header_row,
            "rows": body,
            "row_count": len(body),
            "mapping": {field: index for field, index in mapping.items()},
            "mapping_confidence": confidence,
            "unmapped": unmapped,
        })

    return results


def extract_tables(file_or_path):
    """
    Extract every table from a PDF, joining tables split across page breaks.

    Returns a list of dicts: {index, page, role, confidence, headers, rows,
    mapping, mapping_confidence, unmapped}.
    """
    with pdfplumber.open(file_or_path) as pdf:
        raw_tables = []
        for page_number, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table in tables:
                raw_tables.append((page_number, table))
            if len(raw_tables) >= MAX_TABLES:
                break

    return classify_tables(raw_tables)


# ===========================================================================
# Payload construction
# ===========================================================================
def _cell(row, mapping, field):
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return clean_cell(row[index])


def _resolve_start_end(calendar_lookup, timing_raw):
    """Last resort: pull two times out of whichever calendar cell holds them."""
    candidates = [timing_raw] if timing_raw else []
    for key in ("start time", "college start", "classes start", "timing start",
                "morning start", "day start", "end time", "college end", "classes end",
                "timing end", "day end"):
        if key in calendar_lookup:
            candidates.append(calendar_lookup[key])
    for candidate in candidates:
        start, end = parse_time_range(candidate)
        if start:
            return start, end
    return None, None


def _lookup(calendar_lookup, field, aliases):
    """Exact key match first, then a substring match, longest alias wins."""
    for alias in (field,) + tuple(aliases):
        if alias in calendar_lookup and calendar_lookup[alias]:
            return calendar_lookup[alias]
    keys = list(calendar_lookup)
    for alias in sorted((field,) + tuple(aliases), key=len, reverse=True):
        for key in keys:
            if alias and alias in key and calendar_lookup[key]:
                return calendar_lookup[key]
    return ""


def _lookup_exact(calendar_lookup, field, aliases):
    """Only accept an exact normalised key; avoids 'college timing' being read as a name."""
    for alias in (field,) + tuple(aliases):
        if alias in calendar_lookup and calendar_lookup[alias]:
            return calendar_lookup[alias]
    return ""


def _key_value_pairs(rows):
    """Read a two-column label/value table into a dict of normalized keys."""
    pairs = {}
    for row in rows:
        if len(row) < 2:
            continue
        key = normalize_header(row[0])
        value = clean_cell(row[1])
        if key and value:
            pairs.setdefault(key, value)
    return pairs


def extract_document_title(file_or_path):
    """
    Best guess at the college name: the first substantial text line on page one,
    which in real documents is the letterhead heading.
    """
    try:
        with pdfplumber.open(file_or_path) as pdf:
            if not pdf.pages:
                return ""
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return ""
    for line in text.splitlines():
        candidate = clean_cell(line)
        if len(candidate) >= 6 and not re.fullmatch(r"[\d\s./:-]+", candidate):
            return candidate
    return ""


def build_payload(tables, overrides=None, document_title=""):
    """
    Turn mapped PDF tables into the canonical college payload dictionary.

    ``overrides`` lets the administrator fill in or correct values from the
    import screen (college name, dates, timings, room counts, and so on).
    """
    overrides = overrides or {}
    warnings = []

    calendar = {}
    subject_rows = []
    room_rows = []
    allocation_tables = []

    for table in tables:
        role = table["role"]
        mapping = table.get("mapping") or {}
        rows = table.get("rows") or []
        threshold = MIN_CONFIDENCE.get(role, 50)
        if table.get("mapping_confidence", 0) < threshold:
            warnings.append(
                f"Table on page {table.get('page')} was ignored: recognised as "
                f"'{role}' with only {table.get('mapping_confidence', 0)}% confidence."
            )
            continue
        if role == ROLE_CALENDAR:
            # Key/value tables often have no header row, so pdfplumber reports the
            # first pair as the header. Recover it as data.
            calendar.update(_key_value_pairs([table["headers"]]))
            calendar.update(_key_value_pairs(rows))
        elif role == ROLE_SUBJECTS:
            subject_rows.extend((mapping, row) for row in rows)
        elif role == ROLE_ROOMS:
            room_rows.extend((mapping, row) for row in rows)
        elif role == ROLE_ALLOCATIONS:
            allocation_tables.append((mapping, rows))

    # ---------------- College / calendar block ----------------------------
    college = {}
    calendar_lookup = dict(calendar)

    college["name"] = (overrides.get("college_name")
                       or _lookup_exact(calendar_lookup, "college name",
                                        ("college", "institution", "university", "school",
                                         "institute", "college name"))
                       or (clean_cell(document_title) if document_title else "")
                       or "Imported College")
    college["semester_name"] = (overrides.get("semester_name")
                                or _lookup(calendar_lookup, "semester name",
                                           ("semester name", "semester", "term", "session",
                                            "academic session", "academic term"))
                                or "Imported Semester")

    start_raw = overrides.get("start_date") or _lookup(
        calendar_lookup, "start date", ("start date", "commences on", "commencing",
                                        "commencement", "commence", "from date",
                                        "session start", "date from", "starts on", "start"))
    end_raw = overrides.get("end_date") or _lookup(
        calendar_lookup, "end date", ("end date", "concludes on", "concluding", "conclude",
                                      "to date", "session end", "date to", "ends on", "end"))
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    if not start_date:
        start_date = datetime(2026, 1, 5).date()
        warnings.append("Start date not found in the PDF; defaulted to 2026-01-05.")
    if not end_date:
        end_date = datetime(2026, 4, 30).date()
        warnings.append("End date not found in the PDF; defaulted to 2026-04-30.")
    if end_date <= start_date:
        end_date = datetime(start_date.year + 1, start_date.month, start_date.day).date()
        warnings.append("End date was not after the start date; defaulted to one year later.")
    college["start_date"] = start_date.isoformat()
    college["end_date"] = end_date.isoformat()

    days = parse_days(overrides.get("working_days") or _lookup(
        calendar_lookup, "working days", ("working days", "week days", "weekday",
                                          "class days", "days")))
    if not days:
        days = ["MON", "TUE", "WED", "THU", "FRI"]
        warnings.append("Working days not found in the PDF; defaulted to Monday to Friday.")
    college["working_days"] = days

    timing_raw = overrides.get("daily_start_time") or _lookup(
        calendar_lookup, "daily start time", ("college timing", "college timings",
                                              "daily start time", "timing", "timings",
                                              "college hours", "class timings"))
    range_start, range_end = parse_time_range(timing_raw)
    daily_start = parse_time(overrides.get("daily_start_time") or "") or range_start
    daily_end = parse_time(overrides.get("daily_end_time") or "") or range_end
    if not daily_end:
        daily_end = _lookup(calendar_lookup, "daily end time",
                            ("daily end time", "end time", "college end"))
    if not daily_start or not daily_end:
        start_time, end_time = _resolve_start_end(calendar_lookup, timing_raw)
        daily_start = daily_start or start_time
        daily_end = daily_end or end_time
    if not daily_start:
        daily_start = "09:00"
        warnings.append("Daily start time not found; defaulted to 09:00.")
    if not daily_end:
        daily_end = "17:00"
        warnings.append("Daily end time not found; defaulted to 17:00.")
    college["daily_start_time"] = daily_start
    college["daily_end_time"] = daily_end

    duration = parse_int(
        overrides.get("period_duration_minutes") or _lookup(
            calendar_lookup, "period duration",
            ("lecture duration minutes", "period duration", "period length", "duration",
             "minutes per period", "lecture duration")),
        default=60,
    )
    college["period_duration_minutes"] = duration if duration > 0 else 60

    lunch_raw = overrides.get("lunch_start") or _lookup(
        calendar_lookup, "lunch start", ("lunch break", "lunch", "break", "lunch timings"))
    range_ls, range_le = parse_time_range(lunch_raw)
    lunch_start = parse_time(overrides.get("lunch_start") or "") or range_ls
    lunch_end = parse_time(overrides.get("lunch_end") or "") or range_le
    if not lunch_start or not lunch_end or lunch_end <= lunch_start:
        lunch_start, lunch_end = "13:00", "14:00"
        warnings.append("Lunch break not found or invalid; defaulted to 13:00-14:00.")
    college["lunch_break"] = {"start_time": lunch_start, "end_time": lunch_end}

    # ---------------- Subjects --------------------------------------------
    subjects = []
    seen_subject_ids = set()
    for mapping, row in subject_rows:
        subject_id = _cell(row, mapping, "subject_id")
        subject_name = _cell(row, mapping, "subject_name")
        if not subject_id:
            subject_id = slugify_subject(subject_name, f"SUB{len(subjects) + 1}")
        if not subject_name:
            subject_name = subject_id
        if not subject_id or subject_id in seen_subject_ids:
            continue
        is_lab = parse_bool(_cell(row, mapping, "is_lab"), default=None)
        if is_lab is None:
            is_lab = any(word in normalize_header(subject_name) for word in LAB_WORDS)
        seen_subject_ids.add(subject_id)
        subjects.append({"id": subject_id, "name": subject_name, "is_lab": bool(is_lab)})

    # ---------------- Allocations -----------------------------------------
    allocation_records = []
    division_strengths = defaultdict(set)

    for mapping, rows in allocation_tables:
        for row in rows:
            teacher_name = _cell(row, mapping, "teacher")
            subject_name = _cell(row, mapping, "subject_name")
            subject_code = _cell(row, mapping, "subject_id")
            year = parse_int(_cell(row, mapping, "year"), default=None)
            div_raw = _cell(row, mapping, "division")
            division = parse_int(div_raw, default=None)
            div_label = div_raw.strip() if div_raw and division is None else None
            hours = parse_int(_cell(row, mapping, "total_hours"), default=None)

            if not teacher_name or not subject_name or year is None:
                continue
            if division is None and not div_label:
                continue
            if hours is None or hours <= 0:
                continue

            is_lab_flag = parse_bool(_cell(row, mapping, "is_lab"), default=None)
            allocation_records.append({
                "teacher": teacher_name,
                "subject_id": subject_code or subject_name,
                "subject_name": subject_name,
                "year": year,
                "division": division,
                "div_label": div_label,
                "hours": hours,
                "is_lab": is_lab_flag,
            })

            strength = parse_int(_cell(row, mapping, "strength"), default=None)
            if strength:
                division_strengths[year].add(strength)

    if not allocation_records:
        raise ValueError(
            "No teacher/subject/division rows could be read from this PDF. "
            "Make sure it contains a table with teacher, subject, year, division "
            "and hours columns."
        )

    # Subject list: merge explicit table with anything only seen in allocations.
    name_to_id = {normalize_header(s["name"]): s["id"] for s in subjects}
    id_to_subject = {s["id"]: s for s in subjects}
    name_to_is_lab = {
        normalize_header(r["subject_name"]): r["is_lab"] for r in allocation_records
    }

    for record in allocation_records:
        key = normalize_header(record["subject_name"])
        resolved = record["subject_id"]
        if resolved not in id_to_subject:
            # Match by name against the subjects table.
            matched_id = name_to_id.get(key)
            if matched_id:
                resolved = matched_id
            else:
                new_id = slugify_subject(record["subject_name"],
                                         f"SUB{len(subjects) + 1}")
                base, suffix = new_id, 1
                while new_id in id_to_subject:
                    suffix += 1
                    new_id = f"{base}{suffix}"
                declared_lab = name_to_is_lab.get(key)
                if declared_lab is None:
                    declared_lab = any(word in key for word in LAB_WORDS)
                subjects.append({
                    "id": new_id,
                    "name": record["subject_name"],
                    "is_lab": bool(declared_lab),
                })
                id_to_subject[new_id] = subjects[-1]
                name_to_id[key] = new_id
                resolved = new_id
        record["subject_id"] = resolved

    # A subject appearing in several rows must get the same lab flag everywhere.
    for subject in subjects:
        declared = name_to_is_lab.get(normalize_header(subject["name"]))
        if declared is not None:
            subject["is_lab"] = bool(declared)

    # ---------------- Years / divisions ------------------------------------
    default_strength = parse_int(overrides.get("default_strength"), default=60)
    years = []
    for year in sorted({r["year"] for r in allocation_records}):
        if year < 1 or year > 4:
            warnings.append(f"Year {year} is outside the supported range 1-4 and was skipped.")
            continue
        year_records = [r for r in allocation_records if r["year"] == year]
        numeric_divisions = sorted({r["division"] for r in year_records if r["division"] is not None})
        labeled_divisions = sorted({r["div_label"] for r in year_records if r["div_label"] is not None})
        strengths = division_strengths.get(year) or {default_strength}
        div_list = [
            {"division_number": d, "strength": max(strengths)} for d in numeric_divisions
        ]
        default_prefix = year_prefix_for(year)
        for label in labeled_divisions:
            div_list.append({"division_label": label, "division_prefix": default_prefix, "strength": max(strengths)})
        years.append({
            "year": year,
            "divisions": div_list,
            "strength_per_division": max(strengths),
        })

    valid_divisions = {
        y["year"]: set() for y in years
    }
    valid_labels = {
        y["year"]: set() for y in years
    }
    for y in years:
        for d in y["divisions"]:
            if "division_number" in d:
                valid_divisions[y["year"]].add(d["division_number"])
            elif "division_label" in d:
                prefix = d.get("division_prefix", year_prefix_for(y["year"]))
                valid_labels[y["year"]].add((prefix, d["division_label"]))
                valid_labels[y["year"]].add(d["division_label"])
    allocation_records = [
        r for r in allocation_records
        if (r["division"] is not None and r["division"] in valid_divisions.get(r["year"], set()))
        or (r["div_label"] is not None and (
            (r.get("division_prefix"), r["div_label"]) in valid_labels.get(r["year"], set())
            or r["div_label"] in valid_labels.get(r["year"], set())
        ))
    ]

    # ---------------- Teachers --------------------------------------------
    max_hours_default = parse_int(overrides.get("default_max_hours"), default=24)
    grouped = defaultdict(list)
    for record in allocation_records:
        grouped[record["teacher"]].append(record)

    teachers = []
    for teacher_name, records in grouped.items():
        max_hours = None
        unavail = set()
        for mapping, rows in allocation_tables:
            for row in rows:
                if _cell(row, mapping, "teacher") != teacher_name:
                    continue
                found = parse_int(_cell(row, mapping, "max_hours"), default=None)
                if found and found > 0:
                    max_hours = max(max_hours or 0, found)
                unavail.update(parse_slots(_cell(row, mapping, "unavailable_slots")))
        if max_hours is None:
            workload = sum(r["hours"] for r in records)
            max_hours = min(40, max(max_hours_default, workload))

        teacher_allocations = []
        for r in records:
            alloc = {
                "year": r["year"],
                "subject_id": r["subject_id"],
                "total_hours_for_semester": r["hours"],
            }
            if r["division"] is not None:
                alloc["division"] = r["division"]
            else:
                alloc["division_label"] = r["div_label"]
                alloc["division_prefix"] = year_prefix_for(r["year"]) if isinstance(r["year"], int) else ""
            teacher_allocations.append(alloc)

        teachers.append({
            "id": slugify_subject(teacher_name, f"T{len(teachers) + 1}")[:24],
            "name": teacher_name,
            "max_hours_per_week": int(max_hours),
            "unavailable_slots": sorted(unavail),
            "allocations": teacher_allocations,
        })

    # ---------------- Rooms ------------------------------------------------
    rooms_cfg = {
        "number_of_regular_rooms": 0,
        "regular_room_capacity": 0,
        "number_of_lab_rooms": 0,
        "lab_room_capacity": 0,
    }
    if room_rows:
        for mapping, row in room_rows:
            room_name = _cell(row, mapping, "room_name")
            if not room_name:
                continue
            capacity = parse_int(_cell(row, mapping, "capacity"), default=60)
            is_lab = parse_bool(_cell(row, mapping, "is_lab"), default=None)
            if is_lab is None:
                is_lab = any(word in normalize_header(room_name) for word in LAB_WORDS)
            if is_lab:
                rooms_cfg["number_of_lab_rooms"] += 1
                rooms_cfg["lab_room_capacity"] = max(rooms_cfg["lab_room_capacity"], capacity)
            else:
                rooms_cfg["number_of_regular_rooms"] += 1
                rooms_cfg["regular_room_capacity"] = max(rooms_cfg["regular_room_capacity"], capacity)

    max_strength = max(
        (div["strength"] for y in years for div in y["divisions"]), default=60
    )
    needs_lab = any(s["is_lab"] for s in subjects)
    if not rooms_cfg["number_of_regular_rooms"]:
        division_count = sum(len(y["divisions"]) for y in years)
        rooms_cfg["number_of_regular_rooms"] = max(division_count, 1)
        rooms_cfg["regular_room_capacity"] = max_strength
        warnings.append(
            "No room table found; assumed one classroom per division."
        )
    if needs_lab and not rooms_cfg["number_of_lab_rooms"]:
        division_count = sum(len(y["divisions"]) for y in years)
        rooms_cfg["number_of_lab_rooms"] = max(2, -(-division_count // 4))
        rooms_cfg["lab_room_capacity"] = max_strength
        warnings.append(
            "Laboratory subjects exist but no lab rooms were listed; "
            f"assumed {rooms_cfg['number_of_lab_rooms']} lab rooms."
        )

    if overrides.get("number_of_regular_rooms"):
        rooms_cfg["number_of_regular_rooms"] = parse_int(overrides["number_of_regular_rooms"], 0)
    if overrides.get("regular_room_capacity"):
        rooms_cfg["regular_room_capacity"] = parse_int(overrides["regular_room_capacity"], 0)
    if overrides.get("number_of_lab_rooms"):
        rooms_cfg["number_of_lab_rooms"] = parse_int(overrides["number_of_lab_rooms"], 0)
    if overrides.get("lab_room_capacity"):
        rooms_cfg["lab_room_capacity"] = parse_int(overrides["lab_room_capacity"], 0)

    # --- Warn about rooms that cannot physically seat their divisions -----
    lab_division_strengths = set()
    theory_division_strengths = set()
    for record in allocation_records:
        subject = id_to_subject.get(record["subject_id"])
        if subject is None:
            continue
        strength = next(
            (d["strength"] for y in years for d in y["divisions"]
             if d["division_number"] == record["division"]),
            None,
        )
        if strength is None:
            continue
        (lab_division_strengths if subject["is_lab"] else theory_division_strengths).add(strength)

    if lab_division_strengths and rooms_cfg["lab_room_capacity"]:
        too_big = sorted(s for s in lab_division_strengths if s > rooms_cfg["lab_room_capacity"])
        if too_big:
            warnings.append(
                f"Division strength {max(too_big)} exceeds the largest lab capacity "
                f"({rooms_cfg['lab_room_capacity']}), so laboratory classes for those "
                f"divisions cannot be scheduled. Add a larger lab or lower the "
                f"divisions' strength."
            )
    if theory_division_strengths and rooms_cfg["regular_room_capacity"]:
        too_big = sorted(s for s in theory_division_strengths if s > rooms_cfg["regular_room_capacity"])
        if too_big:
            warnings.append(
                f"Division strength {max(too_big)} exceeds the largest classroom "
                f"capacity ({rooms_cfg['regular_room_capacity']}). Add a larger classroom."
            )

    return {
        "college": college,
        "years": years,
        "subjects": subjects,
        "rooms": rooms_cfg,
        "teachers": teachers,
        "_warnings": warnings,
    }