"""
In-app notifications, and the change detection behind them.

Two audiences:

* **Administrators** are told when the solver cannot place every requested hour,
  and when a remedy is waiting for their approval.
* **Teachers and students** are told when a change actually alters *their* own
  timetable. A message is only worth sending if it names a real difference, so
  the notification layer diffs the timetable before and after and stays silent
  when nothing that matters to that person moved.
"""

from collections import defaultdict

from django.contrib.auth.models import User
from django.urls import reverse

from .models import (
    Assignment,
    Notification,
    ProposedChange,
    SchedulingIssue,
    Student,
    Teacher,
    TimetableEntry,
)
from .roles import is_admin


def _admin_users():
    """Every account that should receive administrator notifications."""
    return list(
        User.objects.filter(is_active=True).filter(
            is_staff=True
        )
    ) + [
        u for u in User.objects.filter(is_active=True, groups__name="Administrators").distinct()
    ]


def _deduplicate(users):
    return list({u.pk: u for u in users}.values())


def notify_admins_about_issues(semester):
    """
    Tell administrators about unresolved scheduling issues.

    Only issues nobody has been told about yet produce a message, so re-running
    the solver does not spam the inbox with the same problems.
    """
    created = []
    url = reverse("scheduler:issue_centre") + f"?semester_id={semester.id}"
    admins = _deduplicate(_admin_users())

    if not admins:
        return created

    issues = list(
        SchedulingIssue.objects.filter(semester=semester).select_related(
            "assignment__division", "assignment__subject", "assignment__teacher"
        )
    )
    for issue in issues:
        already = Notification.objects.filter(
            recipient__in=admins,
            kind="ISSUE",
            semester=semester,
            title__startswith="Cannot schedule",
            is_read=False,
        ).exists()
        if already:
            continue

        short_by = issue.hours_requested - issue.hours_scheduled
        for admin in admins:
            created.append(Notification.objects.create(
                recipient=admin,
                kind="ISSUE",
                severity="critical" if short_by else "warning",
                title=(
                    f"Cannot schedule {short_by}h of "
                    f"{issue.assignment.subject.name} for "
                    f"{issue.assignment.division.name}"
                ),
                body=issue.reason,
                url=url,
                semester=semester,
            ))
    return created


def notify_admins_about_proposals(proposals):
    """Tell administrators that remedies are waiting for approval."""
    if not proposals:
        return []

    admins = _deduplicate(_admin_users())
    semester = proposals[0].semester
    url = reverse("scheduler:proposal_list") + f"?semester_id={semester.id}"
    created = []
    for proposal in proposals:
        for admin in admins:
            created.append(Notification.objects.create(
                recipient=admin,
                kind="PROPOSAL",
                severity="info",
                title=f"Suggested fix: {proposal.title}",
                body=proposal.expected_effect or proposal.rationale,
                url=url,
                semester=semester,
                proposal=proposal,
            ))
    return created


def snapshot_timetable(semester):
    """
    Capture the current timetable as a comparable structure.

    Keyed by ``(assignment_id, slot_code)`` with the room name as the value, so
    a later snapshot can be compared to see exactly what moved.
    """
    rows = (
        TimetableEntry.objects.filter(semester=semester)
        .select_related("room", "time_slot", "assignment__division", "assignment__subject")
        .values_list("assignment_id", "time_slot__day", "time_slot__period_number", "room__name")
    )
    return {
        (assignment_id, f"{day}-P{number}"): room
        for assignment_id, day, number, room in rows
    }


def diff_timetable(before, after, semester):
    """
    Compare two timetable snapshots.

    Returns a mapping of user id to a list of human readable change lines, so
    each person is only told about their own changes.

    Removed and added sessions are paired *per assignment*, so an assignment
    with several weekly periods reports the right moves rather than an
    arbitrary one.
    """
    removed = {k: v for k, v in before.items() if k not in after}
    added = {k: v for k, v in after.items() if k not in before}
    moved_rooms = {
        k: (before[k], after[k])
        for k in before.keys() & after.keys()
        if before[k] != after[k]
    }

    if not (removed or added or moved_rooms):
        return {}

    # moved_rooms is keyed by (assignment_id, slot_code), so take the ids too.
    involved = (
        {k[0] for k in removed}
        | {k[0] for k in added}
        | {k[0] for k in moved_rooms}
    )
    assignments = {
        a.id: a for a in Assignment.objects.filter(id__in=involved).select_related(
            "subject", "teacher", "division"
        )
    }

    removed_by_assignment = defaultdict(list)
    for (assignment_id, slot_code), room in removed.items():
        removed_by_assignment[assignment_id].append(slot_code)
    added_by_assignment = defaultdict(list)
    for (assignment_id, slot_code), room in added.items():
        added_by_assignment[assignment_id].append(slot_code)

    lines_for_teacher = defaultdict(list)
    lines_for_division = defaultdict(list)

    for assignment_id, old_slots in removed_by_assignment.items():
        assignment = assignments.get(assignment_id)
        if assignment is None:
            continue
        new_slots = sorted(added_by_assignment.get(assignment_id, []))
        old_slots = sorted(old_slots)

        paired = min(len(old_slots), len(new_slots))
        for index in range(paired):
            lines_for_teacher[assignment.teacher.user_id].append(
                f"{assignment.subject.name} for {assignment.division.name} moved "
                f"from {old_slots[index]} to {new_slots[index]}."
            )
            lines_for_division[assignment.division_id].append(
                f"{assignment.subject.name} moved from {old_slots[index]} "
                f"to {new_slots[index]}."
            )
        for slot_code in old_slots[paired:]:
            lines_for_teacher[assignment.teacher.user_id].append(
                f"{assignment.subject.name} for {assignment.division.name} at "
                f"{slot_code} was removed."
            )
            lines_for_division[assignment.division_id].append(
                f"{assignment.subject.name} at {slot_code} was removed."
            )
        for slot_code in new_slots[paired:]:
            lines_for_teacher[assignment.teacher.user_id].append(
                f"{assignment.subject.name} for {assignment.division.name} was "
                f"newly scheduled at {slot_code}."
            )
            lines_for_division[assignment.division_id].append(
                f"{assignment.subject.name} was newly scheduled at {slot_code}."
            )

    for (assignment_id, slot_code), (old_room, new_room) in moved_rooms.items():
        assignment = assignments.get(assignment_id)
        if assignment is None:
            continue
        lines_for_teacher[assignment.teacher.user_id].append(
            f"{assignment.subject.name} for {assignment.division.name} at "
            f"{slot_code} moved from {old_room} to {new_room}."
        )
        lines_for_division[assignment.division_id].append(
            f"{assignment.subject.name} at {slot_code} moved from {old_room} "
            f"to {new_room}."
        )

    # Division lines fan out to every enrolled student.
    resolved = {}
    for user_id, lines in lines_for_teacher.items():
        if user_id is not None:
            resolved[user_id] = lines
    for division_id, lines in lines_for_division.items():
        for student in Student.objects.filter(division_id=division_id, is_active=True):
            resolved[student.user_id] = lines

    return {user_id: lines for user_id, lines in resolved.items() if lines}


def notify_timetable_changed(semester, before, approved_title="", actor=None):
    """
    Tell every affected teacher and student exactly what changed for them.
    """
    changes = diff_timetable(before, snapshot_timetable(semester), semester)
    if not changes:
        return []

    url = reverse("scheduler:my_timetable") + f"?semester_id={semester.id}"
    suffix = f" (after approving: {approved_title})" if approved_title else ""
    created = []

    for user_id, lines in changes.items():
        recipient = User.objects.filter(pk=user_id, is_active=True).first()
        if recipient is None:
            continue
        # Keep the message short: name the most important few changes.
        body_lines = lines[:6]
        if len(lines) > len(body_lines):
            body_lines.append(f"...and {len(lines) - len(body_lines)} more change(s).")
        created.append(Notification.objects.create(
            recipient=recipient,
            kind="TIMETABLE_CHANGED",
            severity="warning",
            title=f"Your timetable has changed{suffix}",
            body="\n".join(f"• {line}" for line in body_lines),
            url=url,
            semester=semester,
        ))
    return created


def notify_issue_resolved(semester, proposal, recovered_hours, actor=None):
    """Confirm a remedy worked, so the admin gets a clear outcome."""
    admins = _deduplicate(_admin_users())
    url = reverse("scheduler:issue_centre") + f"?semester_id={semester.id}"
    outcome = (
        f"Recovered {recovered_hours} hour(s)/week."
        if recovered_hours
        else "Applied, but it recovered no hours this time."
    )
    return [
        Notification.objects.create(
            recipient=admin,
            kind="RESOLVED",
            severity="success" if recovered_hours else "warning",
            title=f"Fix applied: {proposal.title}",
            body=outcome,
            url=url,
            semester=semester,
            proposal=proposal,
        )
        for admin in admins
    ]


def unread_count(user):
    if not user or not user.is_authenticated:
        return 0
    return Notification.objects.filter(recipient=user, is_read=False).count()