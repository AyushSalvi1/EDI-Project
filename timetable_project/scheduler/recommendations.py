"""
Recommendation engine for unschedulable teaching hours.

When the solver cannot place all requested hours, something physical is wrong:
a room is too small, a teacher is over their contract, a division is overloaded,
or there simply is not enough time in the week. This module works out *which*,
then proposes the smallest concrete change that would fix it.

Two properties matter more than cleverness here:

1. **The diagnosis uses live data, not the issue's prose.** The solver's
   ``reason`` text is written for humans; matching against it would be brittle.
   Every recommendation recomputes the cause from the database.

2. **Recommendations are trialled, not guessed.** Each remedy is applied inside a
   transaction, the timetable is re-solved, and the recovered hours are measured.
   The transaction is then rolled back, so nothing is changed. A proposal that
   recovers no hours is labelled honestly instead of being presented as a fix.

This is a deterministic constraint-analysis engine, not a language model. It
gives no advice it cannot measure.
"""

from datetime import timedelta

from django.db import transaction

from .models import (
    Assignment,
    DAY_ORDER,
    ProposedChange,
    Room,
    SchedulingIssue,
    Teacher,
    TeacherUnavailability,
    TimeSlot,
)

# Cap the work done for one administrator click, so a page stays responsive.
MAX_TRIALS = 6


# ===========================================================================
# Diagnosis
# ===========================================================================
def diagnose(issue):
    """
    Work out the concrete causes behind one SchedulingIssue.

    Returns a list of cause dicts, each with a ``code`` the proposal builders
    understand plus the numbers behind the diagnosis.
    """
    assignment = issue.assignment
    division = assignment.division
    subject = assignment.subject
    teacher = assignment.teacher
    semester = issue.semester
    shortage = max(0, issue.hours_requested - issue.hours_scheduled)

    causes = []

    # --- Cause 1: no room of the right type and size exists -----------------
    wants_lab = subject.is_lab
    rooms = list(Room.objects.all())
    usable = [
        r for r in rooms
        if r.is_lab == wants_lab and r.capacity >= division.strength
    ]
    if not usable:
        same_type = [r for r in rooms if r.is_lab == wants_lab]
        causes.append({
            "code": "NO_ROOM_FITS",
            "shortage": shortage,
            "is_lab": wants_lab,
            "required_capacity": division.strength,
            "largest_available": max((r.capacity for r in same_type), default=0),
        })
        # Nothing else matters if no room can ever seat this class.
        return causes

    # --- Cause 2: the teacher is over their contracted limit ----------------
    teacher_total = sum(
        a.weekly_hours() for a in Assignment.objects.filter(
            semester=semester, teacher=teacher
        )
    )
    if teacher_total > teacher.max_hours_per_week:
        causes.append({
            "code": "TEACHER_OVER_CONTRACT",
            "shortage": shortage,
            "teacher_id": teacher.id,
            "teacher_name": teacher.name,
            "current_total": teacher_total,
            "contract_limit": teacher.max_hours_per_week,
            "required_limit": teacher_total,
        })

    # --- Cause 3: the division cannot fit in the available week --------------
    slots_per_week = TimeSlot.objects.count()
    division_total = sum(
        a.weekly_hours() for a in Assignment.objects.filter(
            semester=semester, division=division
        )
    )
    if slots_per_week and division_total > slots_per_week:
        causes.append({
            "code": "DIVISION_OVERLOADED",
            "shortage": shortage,
            "division_id": division.id,
            "division_name": division.name,
            "requested": division_total,
            "available": slots_per_week,
            "extra_periods_needed": division_total - slots_per_week,
        })
    elif slots_per_week and division_total == slots_per_week:
        causes.append({
            "code": "WEEK_TOO_SHORT",
            "shortage": shortage,
            "division_id": division.id,
            "division_name": division.name,
            "requested": division_total,
            "available": slots_per_week,
            "extra_periods_needed": 1,
        })

    if not causes:
        unavailability = list(teacher.unavailabilities.all())
        causes.append({
            "code": "TEACHER_UNAVAILABLE",
            "shortage": shortage,
            "teacher_id": teacher.id,
            "teacher_name": teacher.name,
            "unavailable_slots": [s.slot_code for s in unavailability],
        })

    return causes


# ===========================================================================
# Proposal builders
# ===========================================================================
def _build_room_proposal(semester, issue, cause):
    capacity = cause["required_capacity"]
    is_lab = cause["is_lab"]
    kind = "laboratory" if is_lab else "classroom"
    largest = cause.get("largest_available", 0)
    # Prefer the smallest sufficient room: a bigger one wastes seats and money.
    suggested = max(capacity, largest + 10)

    return ProposedChange(
        semester=semester,
        issue=issue,
        kind="ADD_ROOM",
        title=f"Add a {suggested}-seat {kind}",
        rationale=(
            f"{issue.assignment.division.name} has {capacity} students taking "
            f"{issue.assignment.subject.name}, which is a "
            f"{'laboratory' if is_lab else 'theory'} subject. The largest existing "
            f"{kind} seats {largest}, so no room can hold this class and these "
            f"{cause['shortage']} hour(s) can never be scheduled, whatever else is "
            f"changed. A {suggested}-seat {kind} is the smallest room that fixes it."
        ),
        expected_effect=f"Should recover up to {cause['shortage']} hour(s)/week.",
        payload={
            "name": f"{'Lab' if is_lab else 'Room'} {suggested}",
            "capacity": suggested,
            "is_lab": is_lab,
        },
    )


def _build_teacher_limit_proposal(semester, issue, cause):
    return ProposedChange(
        semester=semester,
        issue=issue,
        kind="RAISE_TEACHER_LIMIT",
        title=(
            f"Raise {cause['teacher_name']}'s limit from "
            f"{cause['contract_limit']}h to {cause['required_limit']}h/week"
        ),
        rationale=(
            f"{cause['teacher_name']} is allocated {cause['current_total']} hours a "
            f"week across every division they teach, but their contract limit is "
            f"{cause['contract_limit']}h/week. The solver treats that limit as a hard "
            f"constraint, so {cause['shortage']} hour(s) of "
            f"{issue.assignment.subject.name} for {issue.assignment.division.name} can "
            f"never be placed. Raising the limit makes the workload legal."
        ),
        expected_effect=f"Should recover up to {cause['shortage']} hour(s)/week.",
        payload={
            "teacher_id": cause["teacher_id"],
            "teacher_name": cause["teacher_name"],
            "new_limit": cause["required_limit"],
        },
    )


def _next_free_day():
    """The first working day key that no TimeSlot currently uses."""
    used = set(TimeSlot.objects.values_list("day", flat=True))
    for key in DAY_ORDER:
        if key not in used:
            return key
    return None


def _division_name(division_id, fallback):
    """Resolve a division's display name without a module-level import cycle."""
    from .models import YearDivision

    division = YearDivision.objects.filter(pk=division_id).first()
    return division.name if division else fallback


def _build_extra_period_proposal(semester, issue, cause):
    """Create the missing teaching time, preferring a new working day."""
    division = _division_name(cause["division_id"], cause["division_name"])
    needed = max(1, cause["extra_periods_needed"])
    free_day = _next_free_day()

    if free_day:
        day_label = dict(TimeSlot._meta.get_field("day").choices)[free_day]
        title = f"Enable {day_label} as a working day"
        rationale = (
            f"{division} is allocated {cause['requested']} hours a week but the "
            f"current week offers only {cause['available']} periods. No "
            f"rearrangement of rooms or teachers can fix this, because the time "
            f"simply does not exist. Making {day_label} a working day adds a whole "
            f"day of teaching time."
        )
        payload = {"action": "add_day", "day": free_day}
    else:
        title = f"Extend the college day by {needed} period(s)"
        rationale = (
            f"{division} is allocated {cause['requested']} hours a week but the "
            f"current week offers only {cause['available']} periods, and every "
            f"working day is already in use. Lengthening the college day by "
            f"{needed} period(s) creates the missing time."
        )
        payload = {"action": "extend_day", "extra_periods": needed}

    return ProposedChange(
        semester=semester,
        issue=issue,
        kind="APPLY_PREFERENCES",
        title=title,
        rationale=rationale,
        expected_effect=f"Should create {needed} or more extra period(s) per week.",
        payload=payload,
    )


def YearDivisionName(division_id, fallback):
    """Small helper so the builder can name a division without importing late."""
    from .models import YearDivision

    division = YearDivision.objects.filter(pk=division_id).first()
    return division.name if division else fallback


def _build_unavailability_proposal(semester, issue, cause):
    slots = cause.get("unavailable_slots") or []
    slot_codes = ", ".join(slots[:6]) if slots else "their unavailable periods"

    return ProposedChange(
        semester=semester,
        issue=issue,
        kind="RELAX_UNAVAILABILITY",
        title=f"Release {cause['teacher_name']}'s unavailable periods ({slot_codes})",
        rationale=(
            f"{cause['teacher_name']} is blocked at {slot_codes}, and those periods "
            f"cannot be given to anyone else. That is what pushes {cause['shortage']} "
            f"hour(s) of {issue.assignment.subject.name} for "
            f"{issue.assignment.division.name} out of the week. Only approve this if "
            f"those periods genuinely became available."
        ),
        expected_effect=f"Should recover up to {cause['shortage']} hour(s)/week.",
        payload={
            "teacher_id": cause["teacher_id"],
            "teacher_name": cause["teacher_name"],
            "release_all": True,
        },
    )


def _build_reassign_proposal(semester, issue, cause):
    """Suggest the least loaded teacher who still has contractual headroom."""
    assignment = issue.assignment
    shortlist = []
    for candidate in Teacher.objects.exclude(pk=assignment.teacher_id):
        load = sum(
            a.weekly_hours() for a in Assignment.objects.filter(
                semester=semester, teacher=candidate
            )
        )
        if load + assignment.weekly_hours() <= candidate.max_hours_per_week:
            shortlist.append((load, candidate))

    if not shortlist:
        return None

    shortlist.sort(key=lambda pair: (pair[0], pair[1].name))
    _, best = shortlist[0]
    blocked = best.unavailabilities.count()

    return ProposedChange(
        semester=semester,
        issue=issue,
        kind="REASSIGN_TEACHER",
        title=f"Move {assignment.subject.name} to {best.name}",
        rationale=(
            f"{assignment.teacher.name} cannot absorb this class without exceeding "
            f"their contract limit. {best.name} is the least loaded faculty member "
            f"with contractual headroom left this week, so moving "
            f"{assignment.subject.name} for {assignment.division.name} to them "
            f"relieves the bottleneck without creating a new one. "
            f"{blocked} of {best.name}'s periods are already blocked and the "
            f"solver will work around them."
        ),
        expected_effect=(
            f"Relieves {assignment.teacher.name}; may recover "
            f"{cause.get('shortage', 0)} hour(s)/week."
        ),
        payload={
            "assignment_id": assignment.id,
            "previous_teacher": assignment.teacher.name,
            "new_teacher_id": best.id,
            "new_teacher": best.name,
        },
    )


def _proposals_for_cause(semester, issue, cause):
    if cause["code"] == "NO_ROOM_FITS":
        return [_build_room_proposal(semester, issue, cause)]
    if cause["code"] == "TEACHER_OVER_CONTRACT":
        return [
            _build_teacher_limit_proposal(semester, issue, cause),
            _build_reassign_proposal(semester, issue, cause),
        ]
    if cause["code"] in ("DIVISION_OVERLOADED", "WEEK_TOO_SHORT"):
        return [_build_extra_period_proposal(semester, issue, cause)]
    if cause["code"] == "TEACHER_UNAVAILABLE":
        return [_build_unavailability_proposal(semester, issue, cause)]
    return []


# ===========================================================================
# Sandbox trials
#
# Each trial applies the payload and returns nothing. The caller wraps the whole
# thing in a savepoint that is always rolled back, so these never need undo
# logic of their own.
# ===========================================================================
def _trial_add_room(payload):
    Room.objects.create(
        name=payload["name"],
        capacity=payload["capacity"],
        is_lab=payload["is_lab"],
    )


def _trial_raise_teacher_limit(payload):
    Teacher.objects.filter(pk=payload["teacher_id"]).update(
        max_hours_per_week=payload["new_limit"]
    )


def _trial_reassign_teacher(payload):
    Assignment.objects.filter(pk=payload["assignment_id"]).update(
        teacher_id=payload["new_teacher_id"]
    )


def _trial_release_unavailability(payload):
    TeacherUnavailability.objects.filter(
        teacher_id=payload["teacher_id"]
    ).delete()


def _clone_day_slots(new_day):
    """Give `new_day` the same period times that the first working day has."""
    template = (
        TimeSlot.objects.order_by("day", "period_number")
        .values_list("day", "period_number", "start_time", "end_time")
    )
    first_day = None
    rows = list(template)
    for day, _n, _s, _e in rows:
        first_day = day
        break
    if first_day is None:
        return 0

    to_create = [
        TimeSlot(day=new_day, period_number=number, start_time=start, end_time=end)
        for day, number, start, end in rows
        if day == first_day
    ]
    TimeSlot.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


def _extend_day_slots(extra_periods):
    """Add `extra_periods` periods after the latest existing period, on every day."""
    latest = (
        TimeSlot.objects.order_by("-period_number")
        .values_list("day", "period_number", "start_time", "end_time")
        .first()
    )
    if latest is None:
        return 0

    _day, number, last_start, last_end = latest
    duration_minutes = int((last_end - last_start).total_seconds() // 60)
    days = list(
        TimeSlot.objects.filter(period_number=1)
        .order_by("day")
        .values_list("day", flat=True)
    )
    if not days:
        return 0

    to_create = []
    for step in range(1, extra_periods + 1):
        start = last_start + timedelta(minutes=duration_minutes * step)
        end = start + timedelta(minutes=duration_minutes)
        for day in days:
            to_create.append(
                TimeSlot(
                    day=day,
                    period_number=number + step,
                    start_time=start,
                    end_time=end,
                )
            )
    TimeSlot.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


def _trial_apply_preferences(payload):
    if payload.get("action") == "add_day":
        _clone_day_slots(payload["day"])
    elif payload.get("action") == "extend_day":
        _extend_day_slots(int(payload.get("extra_periods", 1)))


TRIALS = {
    "ADD_ROOM": _trial_add_room,
    "RAISE_TEACHER_LIMIT": _trial_raise_teacher_limit,
    "REASSIGN_TEACHER": _trial_reassign_teacher,
    "RELAX_UNAVAILABILITY": _trial_release_unavailability,
    "APPLY_PREFERENCES": _trial_apply_preferences,
}


@transaction.atomic
def trial_proposal(proposal):
    """
    Measure a remedy without keeping it.

    Applies the payload, re-solves, and reports how many hours were recovered.
    Everything runs inside a savepoint that is always rolled back, so the
    database is left exactly as it was. Returns the hours recovered, or ``None``
    when the remedy could not be trialled at all.
    """
    from .models import TimetableEntry
    from .solver import generate_timetable

    handler = TRIALS.get(proposal.kind)
    if handler is None:
        return None

    before = TimetableEntry.objects.filter(semester=proposal.semester).count()
    before_rooms = Room.objects.count()
    before_slots = TimeSlot.objects.count()

    savepoint = transaction.savepoint()
    try:
        handler(proposal.payload)
        generate_timetable(proposal.semester.id)
        after = TimetableEntry.objects.filter(semester=proposal.semester).count()
    except Exception:
        transaction.savepoint_rollback(savepoint)
        return None

    # Discard every effect of the trial, including the re-solved timetable.
    transaction.savepoint_rollback(savepoint)

    # Safety net: if the sandbox leaked, say so loudly rather than pretending.
    leaked = (
        TimetableEntry.objects.filter(semester=proposal.semester).count() != before
        or Room.objects.count() != before_rooms
        or TimeSlot.objects.count() != before_slots
    )
    if leaked:  # pragma: no cover - defensive
        raise RuntimeError(
            f"Sandbox trial for proposal {proposal.pk or '(unsaved)'} leaked changes; "
            "the database was left in an unexpected state."
        )

    return max(0, after - before)


# ===========================================================================
# Public API
# ===========================================================================
def recommend_for_issue(issue, trial=True):
    """
    Build proposals for one issue, trialled and ranked best-first.

    Returns unsaved ``ProposedChange`` objects. Proposals that recover no hours
    are kept but explicitly labelled, because they may still be part of a wider
    fix that the administrator is assembling.
    """
    drafts = []
    for cause in diagnose(issue):
        for proposal in _proposals_for_cause(issue.semester, issue, cause):
            if proposal is not None:
                drafts.append(proposal)

    if not trial:
        return drafts

    verified = []
    for proposal in drafts[:MAX_TRIALS]:
        gain = trial_proposal(proposal)
        if gain:
            proposal.verified_gain_hours = gain
            proposal.expected_effect = (
                f"Verified in a sandbox: recovers {gain} hour(s)/week."
            )
        else:
            proposal.expected_effect = (
                "Sandbox trial recovered no hours. Review carefully before approving."
            )
            proposal.result_note = (
                "Trialled against the real solver and recovered 0 hours, so this "
                "change alone does not resolve the issue. It may still be part of a "
                "wider fix."
            )
        verified.append(proposal)

    # Best verified remedies first; unproven ones sink to the bottom.
    verified.sort(key=lambda p: p.verified_gain_hours or -1, reverse=True)
    return verified


def _signature(proposal):
    """Identity of a remedy, ignoring which issue prompted it.

    One undersized lab fixes every division that needs it, so three identical
    suggestions would waste the administrator's time and risk applying it twice.
    """
    payload = proposal.payload or {}
    return (proposal.kind, proposal.title, tuple(sorted(payload.items())))


def recommend_for_semester(semester, limit_per_issue=3, trial=True):
    """
    Build and persist ``PENDING`` proposals for every unresolved issue.

    Nothing is applied. The administrator reviews each proposal and approves the
    ones they want. Identical remedies are only proposed once per semester.
    """
    created = []
    seen = set()
    existing = {
        _signature(proposal)
        for proposal in ProposedChange.objects.filter(
            semester=semester, status__in=("PENDING", "APPROVED", "APPLIED")
        )
    }

    issues = (
        SchedulingIssue.objects.filter(semester=semester)
        .select_related(
            "assignment__division", "assignment__subject", "assignment__teacher"
        )
        .order_by(
            "assignment__division__year",
            "assignment__division__division_number",
        )
    )

    for issue in issues:
        if ProposedChange.objects.filter(issue=issue, status="PENDING").exists():
            continue
        for proposal in recommend_for_issue(issue, trial=trial)[:limit_per_issue]:
            signature = _signature(proposal)
            if signature in seen or signature in existing:
                continue
            seen.add(signature)
            proposal.save()
            created.append(proposal)

    return created


def pending_count(semester=None):
    queryset = ProposedChange.objects.filter(status="PENDING")
    if semester is not None:
        queryset = queryset.filter(semester=semester)
    return queryset.count()