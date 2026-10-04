# Automatic Timetable Generator: Complete Project Explanation & Inner Workings

This document is a comprehensive, single-file explanation of **what this project does**, **how each part works under the hood**, and **how to explain it during a demonstration or project viva**.

---

## 1. What This Project Exactly Does

Generating a college timetable manually is notoriously difficult:
- Teachers teach across multiple years (e.g., 1st year physics and 3rd year electronics).
- Rooms are limited (theory classrooms vs. specialized labs).
- Different cohorts (divisions) cannot have classes scheduled at the same time.
- Working hours and lunch breaks must be respected.
- Teachers have part-time constraints or leaves on certain days/periods.

### The Solution:
This application takes either **one single JSON file** or **the college's own published timetable PDF**, and:
1. **Parses & Validates** the input data safely inside a database transaction.
2. **Populates the SQLite Database** with semesters, divisions, rooms, subjects, and teacher allocations.
3. **Runs Google OR-Tools CP-SAT (Constraint Programming)** to mathematically eliminate all schedule clashes.
4. **Generates a Complete Weekly Timetable Grid** (Day $\times$ Period) for every single division in the college (1st, 2nd, 3rd, and 4th year).
5. **Provides Honest Diagnostics**: If an admin requests more hours than physically possible, the solver does not crash—it places the maximum conflict-free hours and outputs a clear list of `SchedulingIssue` rows explaining why and suggesting fixes.
6. **Enables Instant Re-generation**: The admin can change any teacher or allocation in the Django admin and click "Regenerate" to produce an updated timetable in seconds.

---

## 2. End-to-End System Workflow

```
[ college_data.json ]  OR  [ college_timetable.pdf ]
        │                            │
        │                            ▼
        │          [ 0. PDF Import (scheduler/pdf_import.py) ]
        │            • pdfplumber extracts every ruled table.
        │            • Tables classified as calendar / subjects / rooms / allocations.
        │            • Columns matched by header wording, not position.
        │            • Tables split across page breaks rejoined.
        │            • Admin reviews role + column mapping before committing.
        │                            │
        └──────────────┬─────────────┘
                       ▼
[ 1. Validation & Importer (scheduler/importer.py) ]
  • Checks keys, date logic, HH:MM formats, division/subject references.
  • Runs in transaction.atomic() -> if anything fails, zero dirty data.
        │
        ▼
[ 2. Database Models (scheduler/models.py) ]
  • Semester, YearDivision, Subject, Room, TimeSlot, Teacher, Assignment.
  • Excludes lunch break window completely during TimeSlot creation.
        │
        ▼
[ 3. Constraint Solver (scheduler/solver.py) ]
  • Phase 1 (CP-SAT): assigns periods. x[assignment, slot]
  • Phase 2 (matching): assigns rooms by augmenting paths.
  • Posts 7 Hard Constraints (no teacher overlap, no room overlap, etc.)
  • Sets Soft Objective: Maximize sum(x) (places maximum conflict-free hours)
        │
        ▼
[ 4. Output Storage & Diagnostics ]
  • Writes TimetableEntry rows (protected by DB-level unique constraints)
  • Detects shortfalls -> writes SchedulingIssue (Reason + Actionable Suggestion)
        │
        ▼
[ 5. Web Interface (scheduler/views.py & templates) ]
  • /            -> Dashboard with college metrics
  • /upload/pdf/ -> Import from the college's PDF (with mapping review)
  • /upload/     -> Upload JSON file through browser form
  • /capacity/   -> Is this college physically schedulable? Before solving.
  • /generate/   -> 1-click solver run & issues table
  • /timetable/  -> Interactive Day x Period grid (Print/PDF ready)
  • /export/*.pdf-> Division, full-institution, faculty-load, issues PDFs
  • /admin/      -> Faculty reassignment with instant regeneration
```

---

## 3. How It Works Under The Hood (Component by Component)

### A. The Database Layer (The 13 Exact Models)
1. **`Semester`**:
   - Stores `name`, `start_date`, and `end_date`.
   - Method `number_of_weeks()` computes: `max(1, (end_date - start_date).days // 7)`.
2. **`YearDivision`**:
   - Stores `year` (1–4), `division_number` (1, 2, ...), and `strength`.
   - Automatically names itself on save: e.g. `1st Year - Division 2`.
   - Static method `generate_for_year(year, count, strength)` continues sequential numbering from however many already exist for that year.
3. **`Subject`**:
   - Stores `name`, `is_lab` (boolean), and optional `code` (e.g. `SUB101`).
4. **`Room`**:
   - Stores `name`, `is_lab` (boolean), and `capacity`.
   - Static method `generate_rooms(...)` creates sequential names (`Room 1`, `Room 2` for classrooms; `Lab 1`, `Lab 2` for labs).
5. **`TimeSlot`**:
   - Stores `day` (`MON`, `TUE`, `WED`, `THU`, `FRI`, `SAT`), `period_number`, `start_time`, `end_time`.
   - `unique_together = ('day', 'period_number')`.
   - Created by helper `generate_time_slots(...)`: divides daily working hours into equal duration slots, **completely skipping the lunch break window**. Periods are numbered continuously (e.g., periods 1–4 run 9:00–13:00, lunch is 13:00–14:00, period 5 starts at 14:00).
6. **`Teacher`**:
   - Stores `name` and `max_hours_per_week`.
7. **`TeacherUnavailability`**:
   - Maps a `Teacher` to specific `TimeSlot`s when they cannot teach.
8. **`Assignment`** (The Core Unit):
   - Links `Semester`, `Teacher`, `Subject`, and `YearDivision`.
   - Method `weekly_hours()` calculates: $\lceil \text{total\_hours\_for\_semester} / \text{semester.number\_of\_weeks()} \rceil$.
   - Unique together prevents duplicate assignment rows for the same cohort.
9. **`TimetableEntry`** (The Solver Output):
   - Links `Semester`, `Assignment`, `Room`, and `TimeSlot`.
   - Database-level constraint `unique_together = ('semester', 'time_slot', 'room')` guarantees two classes can never be stored in the same room at the same time.
10. **`SchedulingIssue`** (Honest Reporting):
    - When an assignment receives fewer hours than requested, records `hours_requested`, `hours_scheduled`, `reason`, and `suggestion`.
11. **`SolverRun`** (Run Audit):
    - Records `status`, `total_hours_scheduled`, `total_hours_requested` and solver diagnostics for each invocation, so a past run can be explained later.
12. **`Student`** (Portal Login):
    - One-to-one with a Django user, holding `roll_number`, `full_name` and a `division`.
    - Scoping the division to the user is what lets the personal timetable view show only that student's classes.
13. **`TimetableChangeLog`** (Edit Audit):
    - Immutable record of every manual move, swap or delete on a generated entry, capturing `action`, `actor`, `reason` and a `snapshot`.

---

### A2. The PDF Importer (`scheduler/pdf_import.py`)

Colleges already publish timetables as PDFs, so re-keying hundreds of allocation
rows into JSON is wasted work. The importer reads that PDF directly.

- **Table recognition**: each extracted table is classified as `calendar`,
  `subjects`, `rooms` or `allocations`, with a confidence score.
- **Column mapping is by header wording, not position**. A PDF listing `Yr`
  before `Faculty Name` still imports correctly.
- **Flexible values**: `03/08/2026`, `2026-08-03`, `03 August 2026`,
  `Semester commences on`, `09:00 am to 05:00 pm`, `Monday to Friday`,
  `Lab`/`Practical`/`P` versus `Theory`, `MON-P2` versus `Monday Period 2`.
- **Page-break rejoining**: an allocation table that spills onto the next page
  is treated as one logical table, which is essential at 30+ divisions.
- **Review before commit**: `/upload/pdf/` shows every table, its role, its
  confidence and a live preview, and lets the administrator correct the role or
  remap any column. The import then runs through the *same* validated atomic
  importer as the JSON path, so there is only one place where data can be wrong.
- **Honest limits**: it reads selectable text, not scanned images (the upload
  screen says so), and it warns when a college's working days cannot be created
  because `TimeSlot` rows are shared globally.

---

### B. The JSON Importer & Validator (`scheduler/importer.py`)
- **Pre-flight Validation**:
  - Validates that dates are in `YYYY-MM-DD` format and `end_date > start_date`.
  - Validates working days (`MON` through `SAT`).
  - Validates 24-hour times (`HH:MM`) and lunch window bounds.
  - Ensures division numbers and subject IDs referenced in teacher allocations actually exist in the file.
- **Transaction Safety**:
  - The entire database insertion is wrapped in `django.db.transaction.atomic()`.
  - If a single check or key is invalid, the entire operation is rolled back, preventing orphaned or corrupt database records.
- **CLI & Web Execution**:
  - CLI: `python manage.py import_college_data <path_to_json>`
  - Web: Upload form at `/upload/` with inline validation error displays.

---

### C. The OR-Tools CP-SAT Solver (`scheduler/solver.py`)

#### 1. Why CP-SAT?
Standard algorithms (like Genetic Algorithms or Random Backtracking) suffer from two major flaws:
- **Heuristics / Genetic Algorithms**: Non-deterministic; often get stuck at 90-95% feasibility with accidental room or teacher clashes.
- **Pure Backtracking**: Exponential time $\mathcal{O}(k^N)$; causes browsers and servers to time out.
- **Google OR-Tools CP-SAT**: Uses SAT (Satisfiability) solving combined with Lazy Clause Generation and Linear Programming cuts. It mathematically proves feasibility or optimality in seconds.

#### 2. The Mathematical Model

The model is deliberately **split into two phases**, because including the room
index inside every decision variable made the model roughly 254,000 variables
for a realistic 30-division college, which did not prove optimality in 17
seconds.

**Phase 1 — periods (CP-SAT).** Model only $(a, t)$: which assignment meets at
which period. This is the genuinely hard part, and the model stays small enough
to reach `OPTIMAL` in seconds.

**Phase 2 — rooms (augmenting-path matching).** Once periods are fixed, room
assignment is a bipartite matching problem, not a search problem. A room is
joined to a class only when the room type matches the subject's lab flag and
the room seats the entire division. Kuhn-style augmenting paths then produce a
maximum matching. This is polynomial and provably optimal, unlike search.

The benchmark went from ~254,000 variables and no solution in 17.19s to
**360 of 360 hours scheduled in 9.36s, zero issues, status `OPTIMAL`**.

- **Variables (Phase 1)**:
  For every assignment $a$ and time slot $t$, we create a Boolean variable:
  $$x_{a, t} \in \{0, 1\}$$
  Variable $x_{a, t} = 1$ means assignment $a$ takes place at time $t$.
  *Variables are only created for slots where the teacher is not unavailable.*

- **The 7 Hard Constraints (Never Broken)**:
  1. **Teacher Concurrency**: A teacher can only be in one room at a given time:
     $$\sum_{a \in \text{Teacher's assignments}, \, r} x_{a, r, t} \le 1 \quad \forall \text{slot } t$$
  2. **Division Concurrency**: A student division can only attend one class at a given time:
     $$\sum_{a \in \text{Division's assignments}, \, r} x_{a, r, t} \le 1 \quad \forall \text{slot } t$$
  3. **Room Concurrency**: A room can only hold one division at a given time:
     $$\sum_{a, \, r} x_{a, r, t} \le 1 \quad \forall \text{room } r, \, \text{slot } t$$
  4. **Room Matching**: Lab subjects strictly in `is_lab=True` rooms; theory lectures strictly in `is_lab=False` rooms.
  5. **Teacher Unavailability**: If teacher marked `FRI-P4` unavailable, no class is scheduled there.
  6. **Valid Slots Only**: All scheduling happens within the existing `TimeSlot` table (which already excludes lunch).
  7. **Requested Hours Cap**: Total scheduled slots for an assignment cannot exceed its `weekly_hours()`.

- **Soft Objective (Why It Never Crashes on Over-Commitment)**:
  Instead of requiring $\sum x = \text{weekly\_hours}$ as a rigid equality (which would cause the solver to return `INFEASIBLE` and produce 0 timetable entries), we set an objective:
  $$\text{Maximize } \sum x_{a, t}$$
  - If all hours can be scheduled, the solver schedules 100% of them.
  - If a teacher is over-booked (e.g. asked to teach 30 hours when they are only available 20 hours), the solver schedules the maximum possible 20 hours, and creates a `SchedulingIssue` for the remaining 10 hours.

- **Intelligent Diagnostics Generation**:
  For each issue, the solver inspects:
  - Is teacher total assigned hours > `max_hours_per_week`? $\rightarrow$ Explains contract limit and suggests reassigning division.
  - Does division total hours > total available slots in week? $\rightarrow$ Explains cohort overload and suggests reducing subject hours.
  - Does teacher have multiple unavailable slots? $\rightarrow$ Suggests relaxing specific teacher unavailabilities.
  - Is every room too small for the division? $\rightarrow$ Names the required seat count, so the administrator knows exactly how big a room to add.

---

### C2. The Capacity Report (`scheduler/capacity.py`)

Before the solver is ever run, this answers whether the college is *physically*
schedulable. Most large-college scheduling failures are not algorithmic, they
are infrastructure: thirty divisions cannot all meet at once in a college with
twenty classrooms. The report compares weekly room demand against supply,
division strength against room capacity, teacher load against contract limits,
and division load against periods available, separating **blocking** findings
(impossible as specified) from warnings and notes, with an overall
`schedulable` / `unschedulable` verdict.

---

### D. The Web Pages & User Experience
1. **Dashboard (`/`)**:
   - Shows college stats (Total Divisions, Rooms, Faculty, Subjects, Slots Scheduled, and Issues).
   - Shows active semester details and quick action buttons.
2. **Import from PDF (`/upload/pdf/`)**:
   - Uploads the college's PDF and shows every table it found.
   - Each table displays its recognised role, a confidence badge and a preview.
   - Role and column mapping can be corrected, and calendar values overridden,
     before anything is written.
   - Specific, non-technical errors for non-PDF files, oversized files,
     unreadable PDFs and PDFs with no tables.
3. **Upload JSON (`/upload/`)**:
   - Clean drag-and-drop file upload interface.
   - Shows the exact JSON schema required with code highlighting.
   - Form validation with clear error messages.
4. **Capacity Report (`/capacity/`)**:
   - States whether the college is physically schedulable *before* solving.
   - Tables room supply vs demand, division strength coverage and utilisation.
   - Blocking findings are separated from warnings, with a remediation suggestion.
5. **Generate Timetable (`/generate/`)**:
   - Select semester and click **"Run CP-SAT Solver"**.
   - If 100% scheduled: Displays green success card.
   - If over-committed: Displays table listing Division, Subject, Teacher, Requested vs Scheduled, Bottleneck Reason, and Actionable Suggestion.
6. **Division Timetable Grid (`/timetable/`)**:
   - Filter by Semester and Year & Division (e.g. *1st Year - Division 1*).
   - Displays full weekly schedule formatted as a Day $\times$ Period grid.
   - Columns = Days (Monday to Saturday); Rows = Periods (1 to 7 with exact times).
   - Cells show Subject, Teacher, Room, and purple `LAB` badge.
   - Includes **Print / PDF** button with print stylesheet that hides web chrome,
     plus direct PDF downloads for this division, all divisions, faculty load and
     the issues report.
6. **Django Admin (`/admin/`)**:
   - Faculty management: Change or reassign a teacher on any `Assignment`.
   - Directly links to the generate page to re-run the solver and view updated schedules.

---

## 4. How to Run and Test This Project

### Step 1: Open Terminal in Project Directory
```powershell
cd timetable_project
```

### Step 2: Apply Migrations
```powershell
python manage.py migrate
```

### Step 3: Run Automated Test Suite
```powershell
python manage.py test
```
*Runs 59 unit & integration tests covering constraints, over-commitment resilience, lunch exclusion, missing rooms, role-based access, manual edits, and the PDF import/export pipeline.*

### Step 4: Import Sample College Data
```powershell
python manage.py import_college_data sample_college_data.json
```

You can instead import the college's own timetable PDF at
**http://127.0.0.1:8000/upload/pdf/**, which detects the tables, shows the column
mapping for review, and then runs the same validated import.

### Step 5: Start Development Server
```powershell
python manage.py runserver
```

### Step 6: Access the Web App
- Open browser: **http://127.0.0.1:8000/**
- Admin panel: **http://127.0.0.1:8000/admin/**. Create an administrator with `python manage.py createsuperuser`.

---

## 5. Sample JSON Structure (Quick Reference)

```json
{
  "college": {
    "name": "Apex Engineering Institute",
    "semester_name": "Fall Semester 2026",
    "start_date": "2026-08-03",
    "end_date": "2026-11-16",
    "working_days": ["MON", "TUE", "WED", "THU", "FRI", "SAT"],
    "daily_start_time": "09:00",
    "daily_end_time": "17:00",
    "period_duration_minutes": 60,
    "lunch_break": { "start_time": "13:00", "end_time": "14:00" }
  },
  "years": [
    { "year": 1, "number_of_divisions": 2, "strength_per_division": 60 }
  ],
  "subjects": [
    { "id": "SUB101", "name": "Physics", "is_lab": false },
    { "id": "SUB101L", "name": "Physics Lab", "is_lab": true }
  ],
  "rooms": {
    "number_of_regular_rooms": 12,
    "regular_room_capacity": 70,
    "number_of_lab_rooms": 5,
    "lab_room_capacity": 60
  },
  "teachers": [
    {
      "id": "T1",
      "name": "Dr. Rohit Sharma",
      "max_hours_per_week": 20,
      "unavailable_slots": ["MON-P1", "FRI-P5"],
      "allocations": [
        { "year": 1, "division": 1, "subject_id": "SUB101", "total_hours_for_semester": 60 }
      ]
    }
  ]
}
```

---

## 6. Frequently Asked Viva / Interview Questions

| Question | Strong Answer |
| :--- | :--- |
| **Q: Why use Google OR-Tools CP-SAT instead of writing custom backtracking?** | *A: Custom backtracking has worst-case exponential time complexity $\mathcal{O}(d^N)$ and easily freezes on college-scale schedules. CP-SAT uses SAT solvers with Conflict-Driven Clause Learning (CDCL), solving complex combinatorial problems in seconds.* |
| **Q: How does the system guarantee zero clashes?** | *A: Through mathematical hard constraints posted to the CP-SAT model: $\sum x \le 1$ for every teacher, division, and room at any given time slot. Additionally, SQLite enforces `unique_together = ('semester', 'time_slot', 'room')` on `TimetableEntry` at the database level.* |
| **Q: What happens if an admin assigns 40 hours of classes to a teacher who can only work 20 hours?** | *A: Instead of crashing or returning zero timetable entries (`INFEASIBLE`), our soft objective (`Maximize sum(x)`) schedules the maximum conflict-free 20 hours. It then creates a `SchedulingIssue` row identifying the exact bottleneck and recommending corrective action.* |
| **Q: How is the lunch break handled?** | *A: The `generate_time_slots` helper completely excludes any interval overlapping the lunch window (`13:00`–`14:00`). Because lunch slots do not exist in the database, classes can never be scheduled during lunch.* |
| **Q: Why is the room assignment not part of the CP-SAT model?** | *A: Carrying the room index inside every decision variable produced a ~254,000-variable model that did not prove optimality in 17 seconds. But once periods are fixed, room assignment is bipartite matching, not search. Splitting it that way — CP-SAT for periods, augmenting paths for rooms — scheduled 360/360 hours in 9.36s with zero issues at `OPTIMAL`.* |
| **Q: How does the PDF importer know which column is which?** | *A: By header wording, not position, so a document that reorders its columns still imports. Every recognised table is then shown to the administrator with its role and a confidence score before anything is written, and the import runs through the same validated atomic transaction as the JSON path.* |
| **Q: What if the college's rooms are simply too small?** | *A: The capacity report flags it before solving, and the solver refuses to overbook a room. The resulting `SchedulingIssue` names the exact seat count required, so the administrator knows precisely how large a room to add.* |
| **Q: What are the known limitations?** | *A: `Teacher`, `Room`, `Subject`, `TimeSlot` and `YearDivision` are global rather than scoped to a college or semester, so a second college with different working hours reuses the existing period grid (the importer warns when this happens). The PDF importer reads selectable text, not scanned images.* |
