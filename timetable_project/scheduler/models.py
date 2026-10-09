import math
from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def ordinal(n: int) -> str:
    """Return ordinal representation of integer n (e.g. 1 -> '1st', 2 -> '2nd')."""
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


YEAR_PREFIXES = {1: "FY", 2: "SY", 3: "TY", 4: "LY"}


def year_prefix_for(year: int) -> str:
    """Return the default display prefix for a given year of study."""
    return YEAR_PREFIXES.get(year, f"{year}Y")


def year_group_prefixes_with_issues(semester):
    """
    Return the display prefixes (e.g. ``['SY', 'TY']``) for year groups that
    have at least one unresolved scheduling issue in *semester*.
    """
    years = (
        SchedulingIssue.objects.filter(semester=semester)
        .values_list("assignment__division__year", flat=True)
        .distinct()
    )
    return [year_prefix_for(y) for y in sorted(years)]


# ===========================================================================
# 1. Semester
# ===========================================================================
class Semester(models.Model):
    name = models.CharField(max_length=150)
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.name} ({self.start_date} to {self.end_date})"

    def number_of_weeks(self) -> int:
        return max(1, (self.end_date - self.start_date).days // 7)


# ===========================================================================
# 2. YearDivision
# ===========================================================================
class YearDivision(models.Model):
    year = models.IntegerField(help_text="Year of study (1-4)")
    division_number = models.IntegerField(help_text="Sequential division number (1, 2, ...)")
    division_label = models.CharField(
        max_length=20, default="",
        help_text="Display label for the division (e.g. A, B, N). Defaults to the division number.",
    )
    division_prefix = models.CharField(
        max_length=10, default="",
        help_text="Name prefix derived from year (SY, TY, SEDA, ...). Auto-filled if blank.",
    )
    strength = models.IntegerField(default=60)
    name = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['year', 'division_number']
        unique_together = ('year', 'division_number')

    def save(self, *args, **kwargs):
        if not self.division_label:
            self.division_label = str(self.division_number)
        if not self.division_prefix:
            self.division_prefix = year_prefix_for(self.year)
        self.name = f"{self.division_prefix} {self.division_label}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or f"{self.division_prefix} {self.division_label}"

    @staticmethod
    def generate_for_year(year: int, number_of_divisions: int, strength: int,
                          division_labels=None, division_prefix=""):
        """
        Auto-creates that many YearDivision rows, numbered sequentially,
        continuing from however many already exist for that year.

        *division_labels* may be a list of display labels (e.g. ["A", "B", ...])
        whose length must equal *number_of_divisions*. When provided, each label
        is paired with a created division; when omitted the division number is
        used as the label.
        """
        existing_count = YearDivision.objects.filter(year=year).count()
        created_divisions = []
        for i in range(1, number_of_divisions + 1):
            div_num = existing_count + i
            kwargs = {
                "year": year,
                "division_number": div_num,
                "strength": strength,
            }
            if division_labels:
                kwargs["division_label"] = division_labels[i - 1]
            if division_prefix:
                kwargs["division_prefix"] = division_prefix
            division = YearDivision.objects.create(**kwargs)
            created_divisions.append(division)
        return created_divisions


# ===========================================================================
# 3. Subject
# ===========================================================================
class Subject(models.Model):
    code = models.CharField(max_length=50, blank=True, null=True)
    name = models.CharField(max_length=150)
    is_lab = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        type_str = "Lab" if self.is_lab else "Theory"
        if self.code:
            return f"{self.name} [{self.code}] ({type_str})"
        return f"{self.name} ({type_str})"


# ===========================================================================
# 4. Room
# ===========================================================================
class Room(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_lab = models.BooleanField(default=False)
    capacity = models.IntegerField(default=60)

    class Meta:
        ordering = ['is_lab', 'name']

    def __str__(self):
        room_type = "Lab" if self.is_lab else "Classroom"
        return f"{self.name} ({room_type}, Cap: {self.capacity})"

    @staticmethod
    def generate_rooms(number_of_regular_rooms: int, regular_room_capacity: int,
                       number_of_lab_rooms: int, lab_room_capacity: int):
        """
        Auto-creates regular and lab Room rows, named sequentially ('Room 1', 'Room 2'...)
        and ('Lab 1', 'Lab 2'...), continuing from however many already exist of each type.
        """
        created_rooms = []
        existing_regular = Room.objects.filter(is_lab=False).count()
        for i in range(1, number_of_regular_rooms + 1):
            room_name = f"Room {existing_regular + i}"
            room = Room.objects.create(
                name=room_name,
                is_lab=False,
                capacity=regular_room_capacity
            )
            created_rooms.append(room)

        existing_labs = Room.objects.filter(is_lab=True).count()
        for i in range(1, number_of_lab_rooms + 1):
            lab_name = f"Lab {existing_labs + i}"
            room = Room.objects.create(
                name=lab_name,
                is_lab=True,
                capacity=lab_room_capacity
            )
            created_rooms.append(room)

        return created_rooms


# ===========================================================================
# 5. TimeSlot
# ===========================================================================
DAY_CHOICES = [
    ('MON', 'Monday'),
    ('TUE', 'Tuesday'),
    ('WED', 'Wednesday'),
    ('THU', 'Thursday'),
    ('FRI', 'Friday'),
    ('SAT', 'Saturday'),
]

DAY_ORDER = {'MON': 1, 'TUE': 2, 'WED': 3, 'THU': 4, 'FRI': 5, 'SAT': 6}


class TimeSlot(models.Model):
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    period_number = models.IntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ['day', 'period_number']
        unique_together = ('day', 'period_number')

    def __str__(self):
        day_display = dict(DAY_CHOICES).get(self.day, self.day)
        return f"{day_display} Period {self.period_number} ({self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')})"

    @property
    def slot_code(self):
        return f"{self.day}-P{self.period_number}"


def _parse_time(t_val):
    if isinstance(t_val, time):
        return t_val
    if isinstance(t_val, str):
        parts = [int(p) for p in t_val.split(':')]
        return time(parts[0], parts[1])
    raise ValueError(f"Invalid time value: {t_val}")


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _minutes_to_time(m: int) -> time:
    return time(m // 60, m % 60)


def generate_time_slots(working_days, daily_start_time, daily_end_time,
                        period_duration_minutes, lunch_break):
    """
    Create all TimeSlot rows implied by a working week.

    Delegates the arithmetic to `plan_time_slots`, which is pure, and only then
    writes. Anything that needs to *preview* a week must use the planner
    directly, because this function has database side effects.
    """
    lunch_break = _normalise_lunch(lunch_break)

    created_slots = []
    for day_key, period_number, start_time, end_time in plan_time_slots(
        working_days, daily_start_time, daily_end_time,
        period_duration_minutes, lunch_break,
    ):
        slot, created = TimeSlot.objects.get_or_create(
            day=day_key,
            period_number=period_number,
            defaults={'start_time': start_time, 'end_time': end_time},
        )
        if not created and (slot.start_time != start_time
                            or slot.end_time != end_time):
            # The week shape changed. get_or_create leaves an existing row
            # untouched, which would silently keep the old timings and publish a
            # grid that disagrees with the preference that produced it.
            slot.start_time = start_time
            slot.end_time = end_time
            slot.save(update_fields=["start_time", "end_time"])
        created_slots.append(slot)
    return created_slots


def _normalise_lunch(lunch_break):
    """Accept a dict, a pair, or None and return the canonical dict form."""
    if not lunch_break:
        return None
    if isinstance(lunch_break, dict):
        return {
            'start_time': lunch_break.get('start_time'),
            'end_time': lunch_break.get('end_time'),
        }
    start, end = lunch_break
    return {'start_time': start, 'end_time': end}


DAY_KEY_MAP = {
    'MON': 'MON', 'MONDAY': 'MON',
    'TUE': 'TUE', 'TUESDAY': 'TUE',
    'WED': 'WED', 'WEDNESDAY': 'WED',
    'THU': 'THU', 'THURSDAY': 'THU',
    'FRI': 'FRI', 'FRIDAY': 'FRI',
    'SAT': 'SAT', 'SATURDAY': 'SAT',
}


def plan_time_slots(working_days, daily_start_time, daily_end_time,
                    period_duration_minutes, lunch_break=None):
    """
    Work out which periods a working week would contain, without writing.

    Returns a list of ``(day, period_number, start_time, end_time)`` tuples. The
    lunch window is excluded completely: no period overlapping it is ever
    produced. Periods are numbered continuously across the day, skipping the
    lunch gap.
    """
    lunch_break = _normalise_lunch(lunch_break)

    start_mins = _time_to_minutes(_parse_time(daily_start_time))
    end_mins = _time_to_minutes(_parse_time(daily_end_time))

    lunch_start_mins = lunch_end_mins = None
    if lunch_break:
        lunch_start_mins = _time_to_minutes(_parse_time(lunch_break['start_time']))
        lunch_end_mins = _time_to_minutes(_parse_time(lunch_break['end_time']))

    plan = []
    for raw_day in working_days:
        day_key = DAY_KEY_MAP.get(str(raw_day).upper().strip())
        if not day_key:
            continue

        curr = start_mins
        period_num = 1

        while curr + period_duration_minutes <= end_mins:
            p_start = curr
            p_end = curr + period_duration_minutes

            if lunch_start_mins is not None and lunch_end_mins is not None:
                # Overlap condition: max(start1, start2) < min(end1, end2)
                if max(p_start, lunch_start_mins) < min(p_end, lunch_end_mins):
                    # Overlaps lunch: skip forward to the end of the break.
                    if curr < lunch_end_mins:
                        curr = lunch_end_mins
                    else:
                        curr += period_duration_minutes
                    continue

            plan.append((
                day_key,
                period_num,
                _minutes_to_time(p_start),
                _minutes_to_time(p_end),
            ))
            period_num += 1
            curr = p_end

    return plan


# ===========================================================================
# 6. Teacher
# ===========================================================================
class Teacher(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='teacher_profile',
        help_text="Linked login account. When set, this teacher can sign in and see only their own timetable.",
    )
    name = models.CharField(max_length=150)
    max_hours_per_week = models.IntegerField(default=24)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} (Max: {self.max_hours_per_week}h/wk)"


# ===========================================================================
# 7. TeacherUnavailability
# ===========================================================================
class TeacherUnavailability(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='unavailabilities')
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='unavailable_teachers')

    class Meta:
        unique_together = ('teacher', 'time_slot')
        ordering = ['teacher', 'time_slot']

    def __str__(self):
        return f"{self.teacher.name} unavailable at {self.time_slot}"


# ===========================================================================
# 8. Assignment (the core scheduling unit)
# ===========================================================================
class Assignment(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='assignments')
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='assignments')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='assignments')
    division = models.ForeignKey(YearDivision, on_delete=models.CASCADE, related_name='assignments')
    total_hours_for_semester = models.IntegerField(default=60)

    class Meta:
        unique_together = ('semester', 'teacher', 'subject', 'division')
        ordering = ['division__year', 'division__division_number', 'subject__name']

    def __str__(self):
        return f"{self.division.name} - {self.subject.name} by {self.teacher.name} ({self.weekly_hours()}h/wk)"

    def weekly_hours(self) -> int:
        num_weeks = self.semester.number_of_weeks()
        return math.ceil(self.total_hours_for_semester / num_weeks)


# ===========================================================================
# 8b. Student (portal login, scoped to one division)
# ===========================================================================
class Student(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='student_profile',
    )
    roll_number = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    division = models.ForeignKey(
        YearDivision,
        on_delete=models.CASCADE,
        related_name='students',
    )
    enrolled_on = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['division__year', 'division__division_number', 'roll_number']

    def __str__(self):
        return f"{self.roll_number} - {self.full_name} ({self.division.name})"


# ===========================================================================
# 9. TimetableEntry (solver output)
# ===========================================================================
class TimetableEntry(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='timetable_entries')
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='timetable_entries')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='timetable_entries')
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='timetable_entries')

    class Meta:
        unique_together = ('semester', 'time_slot', 'room')
        ordering = ['time_slot__day', 'time_slot__period_number']

    def __str__(self):
        return (f"{self.time_slot} | {self.assignment.division.name} | "
                f"{self.assignment.subject.name} | {self.assignment.teacher.name} | {self.room.name}")


# ===========================================================================
# 10. SchedulingIssue (for honest reporting when full hours can't be scheduled)
# ===========================================================================
class SchedulingIssue(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='scheduling_issues')
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='scheduling_issues')
    hours_requested = models.IntegerField()
    hours_scheduled = models.IntegerField()
    reason = models.TextField()
    suggestion = models.TextField()

    class Meta:
        ordering = ['assignment__division', 'assignment__subject']

    def __str__(self):
        return (f"Issue for {self.assignment}: {self.hours_scheduled}/{self.hours_requested} hrs scheduled")


class TimetableChangeLog(models.Model):
    """Audit trail of manual timetable edits made by administrators."""

    ACTION_CHOICES = [
        ('REGEN', 'Solver re-run'),
        ('MOVE', 'Move / Reschedule'),
        ('SWAP', 'Swap two entries'),
        ('DELETE', 'Remove entry'),
    ]

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='change_logs')
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='timetable_changes',
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    detail = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.changed_by.username if self.changed_by else 'system'
        return f"[{self.get_action_display()}] {who} @ {self.created_at:%Y-%m-%d %H:%M} - {self.semester.name}"


class SolverRun(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='solver_runs')
    created_at = models.DateTimeField(auto_now_add=True)
    cpu_percent_before = models.FloatField()
    cpu_percent_after = models.FloatField()
    ram_used_before_mb = models.FloatField()
    ram_used_after_mb = models.FloatField()
    wall_time_seconds = models.FloatField()
    variable_count = models.PositiveIntegerField()
    constraint_count = models.PositiveIntegerField()
    solver_status = models.CharField(max_length=32)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.semester.name} solver run at {self.created_at} ({self.solver_status})"


# ===========================================================================
# 14. SchedulingPreference (administrator-controlled week shape)
# ===========================================================================
class SchedulingPreference(models.Model):
    """
    The week shape an administrator wants, which the solver honours.

    This exists so the admin does not have to re-import college data to change
    working days or period times. Applying a preference regenerates TimeSlot
    rows, so it is deliberately kept separate from the importer.
    """

    semester = models.OneToOneField(
        Semester, on_delete=models.CASCADE, related_name='preference'
    )
    working_days = models.CharField(
        max_length=30, default='MON,TUE,WED,THU,FRI',
        help_text='Comma separated working days.',
    )
    day_start_time = models.TimeField(default=time(9, 0))
    day_end_time = models.TimeField(default=time(17, 0))
    period_duration_minutes = models.PositiveIntegerField(default=60)
    lunch_start_time = models.TimeField(default=time(13, 0))
    lunch_end_time = models.TimeField(default=time(14, 0))
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='preference_changes',
    )

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.semester.name} preference ({self.working_days})"

    def day_list(self):
        """Validated list of day keys, ordered MON..SAT with unknown values dropped."""
        keys = [d.strip().upper()[:3] for d in self.working_days.split(',') if d.strip()]
        valid = [d for d in DAY_ORDER if d in keys]
        return valid

    def period_count(self):
        """Number of periods a day yields, excluding the lunch window."""
        return len(
            plan_time_slots(
                self.day_list(), self.day_start_time, self.day_end_time,
                self.period_duration_minutes,
                (self.lunch_start_time, self.lunch_end_time),
            )
        ) // max(1, len(self.day_list()))

    def validate(self):
        """Raise ValidationError if the preference cannot produce a usable week."""
        if not self.day_list():
            raise ValidationError("Select at least one working day.")
        if self.period_duration_minutes < 15:
            raise ValidationError("Periods must be at least 15 minutes long.")
        if _time_to_minutes(self.day_end_time) <= _time_to_minutes(self.day_start_time):
            raise ValidationError("The college day must end after it starts.")
        if (_time_to_minutes(self.lunch_start_time) < _time_to_minutes(self.day_start_time)
                or _time_to_minutes(self.lunch_end_time) > _time_to_minutes(self.day_end_time)):
            raise ValidationError("The lunch break must fall inside the college day.")
        if _time_to_minutes(self.lunch_end_time) <= _time_to_minutes(self.lunch_start_time):
            raise ValidationError("Lunch must end after it starts.")

        # plan_time_slots is pure, so this check cannot create any rows.
        if not plan_time_slots(
            self.day_list(), self.day_start_time, self.day_end_time,
            self.period_duration_minutes,
            (self.lunch_start_time, self.lunch_end_time),
        ):
            raise ValidationError(
                "These settings leave no teaching periods. Shorten the lunch break "
                "or lengthen the college day."
            )


class DivisionPreference(models.Model):
    """
    An optional per-division override of the college week.

    Lets an administrator say "SY A works Monday to Wednesday and
    uses periods 1 to 4" without changing anyone else's timetable.
    """

    division = models.OneToOneField(
        YearDivision, on_delete=models.CASCADE, related_name='preference'
    )
    working_days = models.CharField(max_length=30, blank=True)
    first_period = models.PositiveIntegerField(default=1)
    last_period = models.PositiveIntegerField(default=0, help_text='0 means no limit.')
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['division__year', 'division__division_number']

    def __str__(self):
        return f"Preference for {self.division.name}"

    def day_list(self):
        keys = [d.strip().upper()[:3] for d in self.working_days.split(',') if d.strip()]
        return [d for d in DAY_ORDER if d in keys]

    def allowed_slots(self, slots=None):
        """Slots this division is permitted to use, given its own preference."""
        slots = TimeSlot.objects.all() if slots is None else slots
        days = self.day_list()
        result = []
        for slot in slots:
            if days and slot.day not in days:
                continue
            if self.first_period and slot.period_number < self.first_period:
                continue
            if self.last_period and slot.period_number > self.last_period:
                continue
            result.append(slot)
        return result


# ===========================================================================
# 15. ProposedChange (recommendation awaiting administrator approval)
# ===========================================================================
class ProposedChange(models.Model):
    """
    A concrete, reviewable remedy for a scheduling problem.

    Nothing here is applied automatically. The recommendation engine creates
    proposals; only an administrator approving one changes the database, and the
    change is then verified by re-solving and notifying everyone affected.
    """

    KIND_CHOICES = [
        ('ADD_ROOM', 'Add a room'),
        ('RAISE_TEACHER_LIMIT', 'Raise a teacher\'s weekly limit'),
        ('REASSIGN_TEACHER', 'Reassign a subject to another teacher'),
        ('REDUCE_HOURS', 'Reduce weekly hours'),
        ('APPLY_PREFERENCES', 'Apply timetable preferences'),
        ('RELAX_UNAVAILABILITY', 'Relax teacher unavailability'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Awaiting review'),
        ('APPROVED', 'Approved'),
        ('APPLIED', 'Applied and verified'),
        ('REJECTED', 'Rejected'),
        ('FAILED', 'Applied but did not help'),
    ]

    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, related_name='proposals'
    )
    issue = models.ForeignKey(
        # SET_NULL, not CASCADE: re-solving deletes and recreates SchedulingIssue
        # rows, and a pending proposal must survive that so it can still be
        # approved. A detached proposal simply loses its link.
        SchedulingIssue, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='proposals',
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    title = models.CharField(max_length=200)
    rationale = models.TextField(
        help_text='Why the engine believes this will resolve the problem.'
    )
    expected_effect = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='PENDING')
    verified_gain_hours = models.IntegerField(
        default=0, blank=True, null=True,
        help_text='Hours recovered when the remedy was trialled in a sandbox.',
    )
    result_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='reviewed_proposals',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.status}] {self.title}"

    @property
    def is_actionable(self):
        return self.status == 'PENDING'

    def affected_people(self):
        """Human readable description of who this change touches."""
        payload = self.payload or {}
        if self.kind == 'ADD_ROOM':
            return 'All divisions needing a larger room'
        if self.kind == 'RAISE_TEACHER_LIMIT':
            return payload.get('teacher_name', 'the teacher')
        if self.kind == 'REASSIGN_TEACHER':
            return payload.get('previous_teacher', 'the teacher')
        if self.kind == 'REDUCE_HOURS':
            return payload.get('division_name', 'the division')
        if self.kind == 'RELAX_UNAVAILABILITY':
            return payload.get('teacher_name', 'the teacher')
        if self.kind == 'APPLY_PREFERENCES':
            return 'Everyone, if slots are regenerated'
        return ''


# ===========================================================================
# 16. Notification (inbox for admins, teachers and students)
# ===========================================================================
class Notification(models.Model):
    """
    An in-app message. Administrators are told about scheduling problems;
    teachers and students are told when their own timetable changes.
    """

    KIND_CHOICES = [
        ('ISSUE', 'Scheduling issue found'),
        ('PROPOSAL', 'A fix is awaiting your approval'),
        ('TIMETABLE_CHANGED', 'Your timetable changed'),
        ('RESOLVED', 'A problem was resolved'),
        ('SYSTEM', 'System message'),
    ]
    SEVERITY_CHOICES = [
        ('info', 'Information'),
        ('warning', 'Warning'),
        ('critical', 'Needs attention'),
        ('success', 'Good news'),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications'
    )
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, default='SYSTEM')
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='info')
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=200, blank=True)
    semester = models.ForeignKey(
        Semester, null=True, blank=True, on_delete=models.CASCADE, related_name='notifications'
    )
    proposal = models.ForeignKey(
        ProposedChange, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='notifications',
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['recipient', 'is_read', '-created_at'])]

    def __str__(self):
        return f"{self.title} -> {self.recipient.username}"
