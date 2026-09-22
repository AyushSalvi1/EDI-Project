# Quality Evidence: E-S-SF-SC-ET-$

Updated: 2026-09-22

This document records implemented evidence for the academic quality framework. Only items implemented in this session are marked complete.

## Environment

**Status: Implemented for solver monitoring; broader energy evidence is Planned.**

Implemented:

- `psutil` is declared in [requirements.txt](timetable_project/requirements.txt).
- Solver instrumentation in [scheduler/solver.py](timetable_project/scheduler/solver.py#L171-L194) captures CPU utilization before/after, process RSS memory before/after, wall-clock solve time, CP-SAT variable count, constraint count, and final solver status.
- Metrics are emitted through the `scheduler.solver` logger configured in [timetable_project/timetable_project/settings.py](timetable_project/timetable_project/settings.py#L140-L145).
- Metrics are persisted in the `SolverRun` model in [scheduler/models.py](timetable_project/scheduler/models.py) and are retrievable through Django Admin via [scheduler/admin.py](timetable_project/scheduler/admin.py).

### Real Evidence: Sample Database Solver Run

Command executed against the existing sample-backed SQLite database:

```text
Solver resource metrics: {'cpu_percent_before': 41.5, 'cpu_percent_after': 46.0, 'ram_used_before_mb': 132.73, 'ram_used_after_mb': 139.61, 'wall_time_seconds': 23.4092, 'variable_count': 32594, 'constraint_count': 3665, 'solver_status': 'OPTIMAL'}
RESULT {'success': True, 'total_hours_scheduled': 64, 'total_hours_requested': 64, 'issues_count': 0, 'message': 'Successfully generated timetable with 64/64 hours scheduled (0 issues reported).'}
EVIDENCE {'cpu_percent_before': 41.5, 'cpu_percent_after': 46.0, 'ram_used_before_mb': 132.73, 'ram_used_after_mb': 139.61, 'wall_time_seconds': 23.4092, 'variable_count': 32594, 'constraint_count': 3665, 'solver_status': 'OPTIMAL'}
```

The same run is stored in `scheduler_solverrun` and can be inspected through the Django Admin `Solver runs` section.

Still Planned: GPU metrics, energy measurement, repeated benchmark methodology, and storage-growth tracking.

## Sustainability

**Status: Partial.**

The project is modular across Django models, the JSON importer, the CP-SAT solver, views, templates, tests, and management commands. The following maintenance plan is based on the components that actually exist in this repository.

### Maintenance & Versioning Plan

| Activity | Cadence | Repository-specific activity | Evidence / owner |
|---|---|---|---|
| Software bug fixes | As needed, reviewed monthly | Reproduce issues with the Django test suite, then update the relevant importer, solver, model, view, or template. | [scheduler/tests.py](timetable_project/scheduler/tests.py); project maintainer |
| Dependency updates | Monthly review; quarterly update window | Review Django, OR-Tools, psutil, and Python compatibility in [requirements.txt](timetable_project/requirements.txt), then run migrations, checks, and tests. | [requirements.txt](timetable_project/requirements.txt); project maintainer |
| Database backup and cleanup | Weekly backup; monthly cleanup review | Back up the SQLite database before imports or migrations; review old `SolverRun`, timetable, and issue records and retain only records needed for evaluation or operations. | `db.sqlite3`; Django migrations; administrator |
| Performance monitoring | Monthly; after major dataset changes | Run the solver with representative college data and review persisted CPU, RAM, wall time, variable count, constraint count, and status in `SolverRun`. | [scheduler/solver.py](timetable_project/scheduler/solver.py); Django Admin |
| Scalability review | Half-yearly | Review SQLite size, solver model growth, timetable generation time, and whether a production deployment should move to a server database or separate worker process. | [scheduler/models.py](timetable_project/scheduler/models.py); project maintainer |
| Versioning and release review | Quarterly | Tag or record a release after tests pass, migrations are applied, and `QUALITY_EVIDENCE.md` is updated with new evidence. | Git history; [QUALITY_EVIDENCE.md](timetable_project/QUALITY_EVIDENCE.md) |

This plan is a documented operating plan, not evidence that automated backups, CI, or cloud scaling already exist. Those remain Planned until implemented.

## Safety

**Status: Partial.**

Existing tests cover solver conflicts, lunch exclusion, overcommitment, missing rooms, and web views. This session added tests for authentication redirects, upload content-type/size rejection, and persisted solver-run evidence in [scheduler/tests.py](timetable_project/scheduler/tests.py#L317-L358). The room-capacity constraint is now enforced during CP-SAT variable creation in [scheduler/solver.py](timetable_project/scheduler/solver.py), so undersized rooms are never feasible assignments.

Evidence command:

```text
C:/Python314/python.exe d:/TimeTableGenerator/timetable_project/manage.py test scheduler.tests.TimetableWebViewsTestCase
Found 7 test(s) ... OK
```

### Room Capacity Evidence

Before the fix, the deliberately undersized-room test used capacity `1` for rooms serving divisions of strength `50`-`60` and produced **64 capacity violations** while reporting `OPTIMAL` and scheduling `64/64` hours.

After the fix, the same test produced:

```text
RESULT {'success': True, 'total_hours_scheduled': 0, 'total_hours_requested': 64, 'issues_count': 20, 'message': 'Successfully generated timetable with 0/64 hours scheduled (20 issues reported).'}
ENTRIES 0
ISSUES 20
CAPACITY_ISSUES ['No room with sufficient capacity is available for 1st Year - Division 1; the division requires at least 60 seats.']
ENTRY_COUNT 0
CAPACITY_VIOLATION_COUNT 0
CAPACITY_VIOLATIONS []
```

The normal sample capacities were also rerun:

```text
NORMAL_RESULT {'success': True, 'total_hours_scheduled': 64, 'total_hours_requested': 64, 'issues_count': 0, 'message': 'Successfully generated timetable with 64/64 hours scheduled (0 issues reported).'}
NORMAL_ENTRIES 64
NORMAL_ISSUES 0
```

The full regression suite remained green: `Found 11 test(s)` and `Ran 11 tests ... OK`.

### Repeated Import Evidence

Before the fix, importing `sample_college_data.json` into an existing database produced:

```text
FIRST IMPORT: 20 assignments
SECOND IMPORT: 0 assignments
ASSIGNMENT_COUNTS [(1, 20), (2, 0)]
```

The importer now maps each import's local division numbers to the newly created divisions while retaining globally unique database division rows. After the fix, three consecutive imports produced:

```text
FIRST IMPORT: 20 assignments
SECOND IMPORT: 20 assignments
THIRD IMPORT: 20 assignments
ASSIGNMENT_COUNTS [(1, 20), (2, 20), (3, 20)]
```

Both the first and second semesters then generated successfully and remained isolated:

```text
FIRST_RESULT: 64/64 hours scheduled, 0 issues
SECOND_RESULT: 64/64 hours scheduled, 0 issues
FIRST_ASSIGNMENTS: 20
SECOND_ASSIGNMENTS: 20
FIRST_ENTRIES_AFTER_FIRST_RUN: 64
FIRST_ENTRIES_AFTER_SECOND_RUN: 64
SECOND_ENTRIES: 64
```

An independent verification of the second semester reported:

```text
SECOND_ENTRIES 64
SECOND_VIOLATION_COUNT 0
SECOND_VIOLATIONS []
```

The full regression suite remained green after this fix: `Found 11 test(s)` and `Ran 11 tests ... OK`.

## Security

**Status: Implemented for the requested controls; encryption at rest and backups remain Planned.**

Implemented:

- Application views require authentication through `@login_required` in [scheduler/views.py](timetable_project/scheduler/views.py#L27-L129).
- `LOGIN_URL` is configured as `/admin/login/` in [timetable_project/settings.py](timetable_project/timetable_project/settings.py#L38), so anonymous requests redirect to the login page.
- Uploads are limited to 5 MB and checked for `.json` extension and JSON content type in [scheduler/views.py](timetable_project/scheduler/views.py#L24-L66).
- Upload failures return controlled user-facing messages instead of raw exception text in [scheduler/views.py](timetable_project/scheduler/views.py#L76-L84).
- `SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS` are environment-controlled in [timetable_project/settings.py](timetable_project/timetable_project/settings.py#L24-L36).
- HTTPS redirect, HSTS, secure session cookies, and secure CSRF cookies are environment-controlled in [timetable_project/settings.py](timetable_project/timetable_project/settings.py#L147-L152).
- Default admin credentials were removed from [README.md](timetable_project/README.md) and [PROJECT_EXPLAINED.md](timetable_project/PROJECT_EXPLAINED.md). Setup now instructs evaluators to run `python manage.py createsuperuser`.

### Deployment Check Evidence

Before this session, `python manage.py check --deploy` reported **7 issues**: development secret key, `DEBUG=True`, missing SSL redirect, missing HSTS, insecure session cookie, insecure CSRF cookie, and development-only email backend.

With production-style environment variables supplied, the final check reported:

```text
System check identified no issues (0 silenced).
```

The local default configuration is intentionally development-friendly. A real deployment must provide a strong `DJANGO_SECRET_KEY`, explicit `DJANGO_ALLOWED_HOSTS`, `DJANGO_DEBUG=False`, HTTPS/HSTS settings, and secure cookies.

## Ethics

**Status: Partial.**

### Privacy and Consent

The application stores the following personal or institutional data:

- Teacher names and weekly teaching limits in `Teacher`.
- Teacher unavailable time slots in `TeacherUnavailability`.
- Course allocation relationships between teachers, subjects, divisions, and semesters in `Assignment`.
- Semester names and dates in `Semester`.
- Division year, division number, student strength, subject names/codes, room names/capacities, time slots, generated timetable entries, scheduling issues, and solver resource runs.
- Django authentication data, including administrator usernames and password hashes, in Django's built-in authentication tables.

This data is needed to construct conflict-free timetables, enforce teacher availability and workload constraints, assign suitable rooms, and explain scheduling shortfalls. Access to the application views is restricted through `@login_required` in [scheduler/views.py](timetable_project/scheduler/views.py), while administrative data is accessed through Django Admin. Data is retained in the configured SQLite database for the active academic scheduling purpose and should be deleted or archived after the institution's retention period; the exact institutional retention period is still **TBD** and must be approved by the data owner.

### Consent, Licensing, and Output Review

The institution should obtain authorization from the college and relevant staff before importing faculty or timetable data. No external personal dataset is bundled beyond the synthetic/sample college data in [sample_college_data.json](timetable_project/sample_college_data.json). The repository now includes the MIT [LICENSE](timetable_project/LICENSE); Django, OR-Tools, SQLite, psutil, and the other listed dependencies are open-source and should remain subject to their respective licenses.

This application generates schedules with a deterministic CP-SAT constraint solver, not a black-box machine-learning model, so there is no ambiguity about AI-generated prose or predictions. Nevertheless, an administrator must review the generated timetable and any `SchedulingIssue` records before publishing it to students or faculty.

## Cost

**Status: Partial.**

The following is an estimate only. No deployment actuals have been recorded.

### Cost & Economic Feasibility

**Estimated — pre-deployment. Actual values: TBD.**

| Component | Estimated Cost | Notes | Actual |
|---|---:|---|---|
| Hardware | TBD — pending deployment | Development is being performed on a local Windows machine with Python 3.14. Final server sizing has not been measured. | TBD |
| Software / License | ₹0 | Django, OR-Tools, SQLite, psutil, and listed Python dependencies are open-source. The repository includes the MIT license. | TBD |
| Cloud Hosting | ₹1,500–₹4,000/month | Assumes a small single VPS/basic application tier with HTTPS and attached or managed storage. Provider selection and deployment are pending. | TBD |
| Development | Excluded / not tracked | Student development effort is excluded from this pre-deployment estimate. | TBD |
| Maintenance | ₹3,000–₹8,000/month | Rough allowance based on dependency reviews, database backup/cleanup, solver monitoring, and half-yearly scalability review in the maintenance plan. | TBD |

## SDG Mapping

| SDG | Applicability | Score | Evidence status |
|---|---|---:|---|
| SDG 4: Quality Education | Directly applicable: supports academic timetable planning. | 3/5 | Existing timetable workflow and solver tests. |
| SDG 9: Industry, Innovation and Infrastructure | Directly applicable: uses CP-SAT optimization for institutional scheduling. | 2/5 | Solver implementation and new resource measurements. |
| Other SDGs | Not Directly Applicable based on current project scope. | — | No force-fit claim made. |
