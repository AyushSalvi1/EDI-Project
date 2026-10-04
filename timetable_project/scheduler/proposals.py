"""
Applying an approved recommendation.

The rule the whole feature rests on: **nothing changes until an administrator
approves it.** This module is the only place that mutates the database on the
strength of a proposal, and it always does so inside one transaction, verifying
the outcome afterwards rather than assuming it.

Sequence for an approval:

1. Snapshot the timetable so the real effect can be measured.
2. Apply the payload.
3. Re-solve, and compare before and after.
4. Record the result on the proposal and in the change log.
5. Notify the teachers and students whose own timetable actually changed.

If applying the payload fails, the transaction rolls back and the proposal is
marked ``FAILED`` rather than leaving a half-applied state behind.
"""

from django.db import transaction

from .models import (
    Assignment,
    ProposedChange,
    Room,
    SchedulingIssue,
    Teacher,
    TeacherUnavailability,
    TimeSlot,
    TimetableChangeLog,
)
from .notifications import (
    notify_issue_resolved,
    notify_timetable_changed,
    snapshot_timetable,
)
from .recommendations import _clone_day_slots, _extend_day_slots
from .solver import generate_timetable


class ProposalError(Exception):
    """Raised when a proposal cannot be applied."""


def apply_payload(proposal):
    """
    Apply a proposal's payload to the database.

    Deliberately small and explicit: one branch per remedy kind, no generic
    reflection over the payload, so it is obvious what each approval changes.
    """
    payload = proposal.payload or {}

    if proposal.kind == "ADD_ROOM":
        if Room.objects.filter(name=payload["name"]).exists():
            raise ProposalError(f"A room called '{payload['name']}' already exists.")
        Room.objects.create(
            name=payload["name"],
            capacity=payload["capacity"],
            is_lab=payload["is_lab"],
        )
        return f"Created room {payload['name']} ({payload['capacity']} seats)."

    if proposal.kind == "RAISE_TEACHER_LIMIT":
        teacher = Teacher.objects.filter(pk=payload["teacher_id"]).first()
        if teacher is None:
            raise ProposalError("That teacher no longer exists.")
        previous = teacher.max_hours_per_week
        teacher.max_hours_per_week = payload["new_limit"]
        teacher.save(update_fields=["max_hours_per_week"])
        return (
            f"{teacher.name}'s weekly limit changed from {previous}h "
            f"to {payload['new_limit']}h."
        )

    if proposal.kind == "REASSIGN_TEACHER":
        assignment = Assignment.objects.filter(pk=payload["assignment_id"]).first()
        if assignment is None:
            raise ProposalError("That assignment no longer exists.")
        teacher = Teacher.objects.filter(pk=payload["new_teacher_id"]).first()
        if teacher is None:
            raise ProposalError("The suggested teacher no longer exists.")
        previous = assignment.teacher.name
        assignment.teacher = teacher
        assignment.save(update_fields=["teacher"])
        return f"{assignment.subject.name} moved from {previous} to {teacher.name}."

    if proposal.kind == "RELAX_UNAVAILABILITY":
        count, _ = TeacherUnavailability.objects.filter(
            teacher_id=payload["teacher_id"]
        ).delete()
        return f"Released {count} unavailable period(s) for {payload['teacher_name']}."

    if proposal.kind == "APPLY_PREFERENCES":
        action = payload.get("action")
        if action == "add_day":
            created = _clone_day_slots(payload["day"])
            if not created:
                raise ProposalError("There are no existing periods to copy from.")
            return f"Added {created} period(s) on {payload['day']}."
        if action == "extend_day":
            created = _extend_day_slots(int(payload.get("extra_periods", 1)))
            if not created:
                raise ProposalError("There are no existing periods to extend.")
            return f"Added {created} extra period(s) to each working day."
        raise ProposalError("This proposal does not describe an applicable change.")

    raise ProposalError(f"Unknown proposal type '{proposal.kind}'.")


def _count_hours(semester):
    from .models import TimetableEntry
    return TimetableEntry.objects.filter(semester=semester).count()


def approve_and_apply(proposal, user):
    """
    Apply an approved proposal, re-solve, and notify whoever is affected.

    Returns a dict describing what happened. The payload and the new timetable
    are committed together; if anything raises, both are discarded.
    """
    if proposal.status != "PENDING":
        raise ProposalError(
            f"This proposal was already {proposal.get_status_display().lower()}."
        )

    semester = proposal.semester
    before_snapshot = snapshot_timetable(semester)
    before_hours = _count_hours(semester)
    before_issues = SchedulingIssue.objects.filter(semester=semester).count()

    try:
        with transaction.atomic():
            summary = apply_payload(proposal)

            # Keep the saved timetable consistent with the new constraints.
            result = generate_timetable(semester.id)

            after_hours = _count_hours(semester)
            after_issues = SchedulingIssue.objects.filter(semester=semester).count()
            recovered = after_hours - before_hours

            proposal.status = "APPLIED" if recovered > 0 or after_issues < before_issues else "FAILED"
            proposal.reviewed_by = user
            proposal.reviewed_at = _now()
            proposal.result_note = (
                f"{summary} "
                f"Scheduled hours {before_hours} -> {after_hours}; "
                f"unresolved issues {before_issues} -> {after_issues}."
            )
            proposal.save(update_fields=[
                "status", "reviewed_by", "reviewed_at", "result_note",
            ])

            TimetableChangeLog.objects.create(
                semester=semester,
                changed_by=user,
                action="REGEN",
                detail=(
                    f"Approved proposal '{proposal.title}'. {summary} "
                    f"{result['total_hours_scheduled']}/"
                    f"{result['total_hours_requested']} hours scheduled."
                ),
            )
    except ProposalError:
        raise
    except Exception as exc:
        proposal.status = "FAILED"
        proposal.reviewed_by = user
        proposal.reviewed_at = _now()
        proposal.result_note = f"Could not be applied: {exc}"
        proposal.save(update_fields=["status", "reviewed_by", "reviewed_at", "result_note"])
        raise ProposalError(str(exc)) from exc

    # The timetable has genuinely changed, so tell the people it changed for.
    notify_timetable_changed(semester, before_snapshot, approved_title=proposal.title)
    if proposal.status == "APPLIED":
        notify_issue_resolved(semester, proposal, after_hours - before_hours, actor=user)

    return {
        "summary": summary,
        "recovered_hours": after_hours - before_hours,
        "hours_before": before_hours,
        "hours_after": after_hours,
        "issues_before": before_issues,
        "issues_after": after_issues,
        "status": proposal.status,
    }


def reject(proposal, user, note=""):
    """Decline a proposal without touching the timetable."""
    if proposal.status != "PENDING":
        raise ProposalError(
            f"This proposal was already {proposal.get_status_display().lower()}."
        )
    proposal.status = "REJECTED"
    proposal.reviewed_by = user
    proposal.reviewed_at = _now()
    proposal.result_note = note or "Rejected by the administrator."
    proposal.save(update_fields=["status", "reviewed_by", "reviewed_at", "result_note"])

    from .notifications import _deduplicate, _admin_users
    from .models import Notification
    from django.urls import reverse

    url = reverse("scheduler:proposal_list") + f"?semester_id={proposal.semester.id}"
    for admin in _deduplicate(_admin_users()):
        Notification.objects.create(
            recipient=admin,
            kind="SYSTEM",
            severity="info",
            title=f"Fix declined: {proposal.title}",
            body=proposal.result_note,
            url=url,
            semester=proposal.semester,
            proposal=proposal,
        )
    return proposal


def _now():
    from django.utils import timezone
    return timezone.now()