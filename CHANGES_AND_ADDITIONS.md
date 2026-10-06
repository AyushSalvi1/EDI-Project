# Project Changes & Additions

**Automatic Timetable Generator** — Django + Google OR-Tools CP-SAT
**Date:** 4 October 2026
**Baseline commit:** `9f4c449` — *Major Changes in the existing project*

---

## 1. Executive Summary

Since the last commit the project gained **6 new features** (~3,700 lines across 25 new
files) and grew by **~2,845 insertions** across 15 existing files. A full audit of every
page and form uncovered **7 real bugs**, all of which are now fixed and covered by
regression tests. An additional **4 real bugs** were identified post-audit and remain open.

| Metric | Before | After |
|---|---|
| Application routes | 15 | 30 |
| Python modules | 12 | 19 |
| Test cases | 59 | 68 |
| Test result | — | 68 passed, 0 failed |
| Known open bugs | 0 | 4 |

---

## 2. New Features Added

### 2.1 PDF Import — `scheduler/pdf_import.py` (975 lines)

Imports a college timetable from a real-world PDF rather than requiring hand-built JSON.

- Parses ruled tables with selectable text using `pdfplumber`
- Tolerates deliberately messy documents: shuffled column order, `DD Mon YYYY` dates,
  working days expressed as a range, the lab flag written as the letter `P`, implied
  rooms, and 12 numbered divisions
- **Two-stage flow.** Stage 1 parses the PDF and reports how each table was recognised.
  Stage 2 lets the administrator correct the column mapping, then performs the exact
  same validated atomic import the JSON path uses
- Runs the identical validation as the JSON importer, so a bad PDF cannot corrupt data

**Route:** `/upload/pdf/` · **Template:** `upload_pdf.html` (208 lines)

### 2.2 PDF Export — `scheduler/pdf_export.py` (445 lines)

Four printable reports rendered with `reportlab`:

| Report | Route |
|---|---|
| Full institution grid | `/export/full.pdf` |
| Faculty teaching load | `/export/faculty.pdf` |
| Scheduling issues | `/export/issues.pdf` |
| Single division | `/export/division/<id>.pdf` |

### 2.3 Recommendations Engine — `scheduler/recommendations.py` (514 lines)

- Diagnoses every unresolved scheduling issue and proposes a concrete remedy
- **Each remedy is trial-solved before being shown**, so the claimed hour gain is
  verified rather than guessed
- Multiple remedy types: add a room, raise capacity, move a teacher, rebalance load
- Identical remedies are de-duplicated — one undersized lab no longer produces three
  identical proposals

### 2.4 Approval Workflow — `scheduler/proposals.py` (190 lines)

- Proposals remain `PENDING` until an administrator explicitly approves them
- Nothing is ever changed automatically
- Approval applies the change inside a transaction, re-solves, and reports the real
  before/after hours and the actual issue count resolved
- Rejection records who declined it and why

**Route:** `/proposals/` · **Template:** `proposals.html` (116 lines)

### 2.5 Week Preferences — `scheduler/preferences.py` (274 lines)

- Set working days, start/end time, period length and lunch break
- **Analyses the proposed week shape before saving** — if it cannot hold the
  curriculum, the change is refused with the specific reasons rather than applied blindly
- Per-division overrides (first/last period, consecutive lab blocks)
- Warns when a shared period grid change damages another semester

**Routes:** `/preferences/`, `/preferences/division/<id>/`

### 2.6 Notifications — `scheduler/notifications.py` (256 lines)

- Teachers and students are told when **their** timetable changes, with a before/after
  diff of what moved
- Administrators are told when issues appear and when proposals await approval
- One indexed unread count per page for the navigation badge, via
  `context_processors.py` (14 lines)

**Route:** `/notifications/` · **Template:** `notifications.html` (112 lines)

### 2.7 Supporting Additions

| File | Lines | Purpose |
|---|---|---|
| `scheduler/capacity.py` | 184 | Reports facility shortfalls *before* running the solver |
| `management/commands/seed_demo_accounts.py` | 149 | One admin, teacher and student login, each bound to real classes |
| `scheduler/templatetags/scheduler_tags.py` | 15 | Template filters for the grid |
| `migrations/0004_...py` | 276 | 9 new models |
| `migrations/0005_...py` | 31 | `SET_NULL` on proposal links |
| 7 new templates | 1,068 | Capacity, division preference, issue centre, notifications, preferences, proposals, PDF upload |

### 2.8 Dependencies Added

```
pdfplumber>=0.11.0    # PDF import: reads ruled tables from a timetable PDF
reportlab>=4.0.0      # PDF export: renders division, faculty-load and issue reports
```

---

## 3. Changes to Existing Files

15 tracked files changed — **2,845 insertions, 353 deletions**.

| File | Change | What it does |
|---|---|---|
| `scheduler/views.py` | +754 | 13 new views; 15 → 30 routes |
| `scheduler/solver.py` | +593 | Backtracking room-matching rounds; records scheduling issues |
| `scheduler/tests.py` | +743 | 59 → 68 test cases |
| `scheduler/models.py` | +378 | 9 new models |
| `scheduler/importer.py` | +84 | Shared by the JSON and PDF import paths |
| `scheduler/urls.py` | +25 | New route table |
| `templates/base.html` | +127 | Navigation, notification badge, confirm dialog |
| `templates/dashboard.html` | +89 | Capacity and issue metrics |
| `templates/timetable_view.html` | +20 | Room and filter display |
| `templates/upload_json.html` | +20 | Validation error reporting |
| `README.md` | +212 | Full rewrite |
| `PROJECT_EXPLAINED.md` | +146 | Architecture notes |
| `requirements.txt` | +6 | Two new dependencies |
| `settings.py` | +1 | Context processor registration |
| `db.sqlite3` | binary | Rebuilt demo data |

---

## 4. Bugs Found and Fixed

A full audit exercised all 30 routes as administrator, teacher, student and anonymous
user, plus every form, the real JSON and PDF importers, and all PDF exports.
**Seven genuine defects were found.**

### Bug 1 — Two features crashed on first use (missing imports)

`SchedulingPreference` was used in `views.py` but never imported; `generate_time_slots`
was used in `preferences.py` but never imported. The preferences page raised
`NameError` on every save. Found with `pyflakes`.

### Bug 2 — "Apply preferences" always crashed

`preferences.py` saved the *candidate* object built from the submitted form — an unsaved
duplicate — so it tried to `INSERT` a second row for a semester that already had one:

```
IntegrityError: UNIQUE constraint failed: scheduler_schedulingpreference.semester_id
```

Now updates the stored row.

### Bug 3 — Week shape never re-timed existing periods

`generate_time_slots` used `get_or_create`, so an existing period kept its original
start and end time. Shrinking the college day to 09:00–15:00 left a period starting at
**15:00 on a day that ended at 15:00**. The published grid silently disagreed with the
preference that produced it. Now changed times are refreshed.

### Bug 4 — Blank time field returned HTTP 500

Clearing the start time field produced `ValueError: invalid literal for int()` with no
handler, so the whole page 500'd. Now raises a proper validation error rendered as a
readable form message.

### Bug 5 — Administrator got a 500 on the personal timetable page

`my_timetable_view` assumed every signed-in user has either a teacher or a student
profile. An administrator has neither, so notification links pointed them at a crash.
Now redirected to their own landing page.

### Bug 6 — Approving a proposal deleted it mid-approval

`ProposedChange.issue` used `on_delete=CASCADE`. Approval re-runs the solver, which
deletes and recreates every `SchedulingIssue` row — taking the proposal being approved
with it. The final save then failed:

```
ProposedChange.NotUpdated: Save with update_fields did not affect any rows.
```

The change applied, but the page returned an error. Changed to `SET_NULL`, so a pending
proposal survives a re-solve.

### Bug 7 — Identical proposals and triple notifications

One undersized laboratory produced three identical "Add a 70-seat laboratory" proposals
— one per affected issue — and six notifications instead of two. Added signature-based
de-duplication; a single approval now resolves every issue that shared the remedy.

### Also corrected

- `notify_timetable_changed` passed composite `(assignment_id, slot_code)` tuples into
  an `id__in` lookup. This crashed *after* the transaction committed, so the fix applied
  but the user saw an error page.
- A stale test asserted a navigation label that no longer existed.

---

## 5. Testing

| | |
|---|---|
| Test cases | **68** (59 existing + 9 new regression tests) |
| Result | **68 passed, 0 failed** |
| Runtime | ~253 seconds |
| Static analysis | `pyflakes` clean — no undefined names |
| Route coverage | 30 of 30 routes exercised in all 4 roles |

### Security verification

Access control was checked exhaustively. **Zero permission leaks:** all 17
administrator-only routes correctly redirect teachers, students and anonymous users.
Open-redirect protection on the login form confirmed working. Logout is POST-only.

### Regression tests added

Each new test names the symptom it prevents, so the defect cannot silently return:

1. Administrator without a profile is redirected, not crashed
2. A proposal survives the re-solve its own approval triggers
3. Applying preferences updates rather than inserts
4. Changing the week shape re-times existing periods
5. Blank time field yields a form error, not a 500
6. Blank period length yields a form error, not a 500
7. Identical remedies are proposed once
8. One approval clears every issue sharing that remedy
9. Recommending twice does not duplicate pending proposals

---

## 6. Demo Accounts

| Role | Username | Password | Lands on |
|---|---|---|---|
| Administrator | `admin` | `admin123` | Dashboard, solver, management |
| Teacher | `teacher.demo` | `Teach@12345` | Personal teaching timetable |
| Student | `student.demo` | `Study@12345` | Personal class timetable |

Change these passwords before any real deployment.

**Required setup order before seeding accounts:**
1. `python manage.py migrate`
2. `python manage.py import_college_data sample_college_data.json`
3. `python manage.py generate_timetable`
4. `python manage.py seed_demo_accounts`

Running `seed_demo_accounts` out of sequence fails silently or confusingly.

**Current demo dataset:** *Fall Semester 2026* — 7 divisions, 11 subjects, 17 rooms,
7 teachers, 20 assignments, 42 periods across 6 days. The solver schedules
**64 of 64 requested hours with 0 unresolved issues.**

---

## 7. Known Limitation

`TimeSlot` is global rather than per-semester, because the code treats the period grid
as an institution-wide shared structure. Importing a second timetable therefore
permanently alters the grid for all semesters. The preferences flow detects this and
warns which other semesters were affected, but if true multi-semester operation is
needed, `TimeSlot` should become semester-scoped.

---

## 8. Known Open Bugs

These defects were identified after the initial 7-bug audit and have not been fixed
at the time of writing.

### Bug 8 — Student dashboard shows "Subjects 0" despite active timetable

`my_timetable_view` (views.py) only populates `hours_by_subject` when the user is a
teacher. For students the dict remains empty, so the summary card always displays 0
subjects even though the grid correctly lists every class.

### Bug 9 — Benchmark scripts use hardcoded absolute paths

`bench_pdf_export.py`, `bench_pdf_import.py` and `bench_scale.py` contain paths
such as `C:\EDI Project\TimeTableGenerator\timetable_project` and
`C:\Users\lenovo\AppData\Local\Temp\kilo\...`. These scripts only run on the original
developer's machine.

### Bug 10 — Benchmark scripts delete database rows without confirmation

`bench_pdf_export.py`, `bench_pdf_import.py` and `bench_scale.py` call
`.all().delete()` on every model at startup. Running any of them against a database
containing real data destroys it irreversibly.

### Bug 11 — `validate_pdf_layout.py` crashes with no arguments

The script reads `sys.argv[1]` unconditionally. Invoking it without a PDF path raises
`IndexError` instead of printing usage.

## 9. Repository Status

All work is **uncommitted**. Nothing has been pushed. `db.sqlite3` is modified and
contains the rebuilt demo dataset; consider whether it belongs in version control.

```
Modified:  15 tracked files
New:      25 untracked files
```