import logging
from django.core.management.base import BaseCommand
from scheduler.models import (
    Semester,
    YearDivision,
    Subject,
    Teacher,
    Room,
    TimeSlot,
    Assignment,
    TeacherUnavailability,
    TimetableEntry,
    SchedulingIssue,
)
from scheduler.solver import generate_timetable

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Inject controlled teacher and classroom conflicts into the current semester to test system conflict handling."

    def add_arguments(self, parser):
        parser.add_argument(
            "--resolve",
            action="store_true",
            help="Remove injected conflict constraints and re-generate a clean timetable.",
        )

    def handle(self, *args, **options):
        resolve_mode = options.get("resolve", False)
        semester = Semester.objects.order_by("-start_date").first()
        if not semester:
            self.stdout.write(self.style.ERROR("No semester found. Please import college data first."))
            return

        self.stdout.write(self.style.NOTICE(f"Operating on semester: {semester.name} (id={semester.id})"))

        if resolve_mode:
            # Clean up injected test teachers/unavailabilities
            self.stdout.write("Restoring baseline configurations...")
            TeacherUnavailability.objects.filter(teacher__name__contains="[Conflict Demo]").delete()
            Assignment.objects.filter(teacher__name__contains="[Conflict Demo]").delete()
            Teacher.objects.filter(name__contains="[Conflict Demo]").delete()
            Room.objects.filter(name__contains="[Conflict Demo]").delete()

            result = generate_timetable(semester.id)
            self.stdout.write(self.style.SUCCESS(f"Successfully resolved and re-solved: {result['message']}"))
            return

        self.stdout.write(self.style.NOTICE("Creating Teacher and Classroom Conflict Scenarios..."))

        # -------------------------------------------------------------
        # 1. TEACHER CONFLICT A: Teacher Unavailability Clash
        # -------------------------------------------------------------
        teacher_unavail_demo, _ = Teacher.objects.get_or_create(
            name="Dr. Suresh Rao [Conflict Demo]",
            defaults={"max_hours_per_week": 16},
        )
        # Block several prime slots (e.g. MON P1, MON P2, MON P3, TUE P1, TUE P2)
        slots_to_block = TimeSlot.objects.filter(day__in=["MON", "TUE"], period_number__in=[1, 2, 3])
        for slot in slots_to_block:
            TeacherUnavailability.objects.get_or_create(
                teacher=teacher_unavail_demo,
                time_slot=slot,
            )
        self.stdout.write(self.style.SUCCESS(f"  [+] Teacher Conflict 1: Added unavailability constraints for {teacher_unavail_demo.name} across {slots_to_block.count()} slots."))

        # -------------------------------------------------------------
        # 2. TEACHER CONFLICT B: Over-Contract Teaching Workload
        # -------------------------------------------------------------
        teacher_overload_demo, _ = Teacher.objects.get_or_create(
            name="Prof. Meera Sen [Conflict Demo]",
            defaults={"max_hours_per_week": 8},  # Very low contract cap
        )
        # Allocate 16 hours of subject teaching to Meera Sen (exceeds 8h limit by 8h)
        sub_ai, _ = Subject.objects.get_or_create(
            name="Advanced AI & Robotics",
            defaults={"is_lab": False, "code": "CS801"},
        )
        div_y4 = YearDivision.objects.filter(year=4).first() or YearDivision.objects.first()
        assign_overload, _ = Assignment.objects.get_or_create(
            semester=semester,
            teacher=teacher_overload_demo,
            subject=sub_ai,
            division=div_y4,
            defaults={"total_hours_for_semester": 120},  # ~8 hrs/week
        )
        sub_ml, _ = Subject.objects.get_or_create(
            name="Machine Learning Applications",
            defaults={"is_lab": False, "code": "CS802"},
        )
        assign_overload2, _ = Assignment.objects.get_or_create(
            semester=semester,
            teacher=teacher_overload_demo,
            subject=sub_ml,
            division=div_y4,
            defaults={"total_hours_for_semester": 120},  # another ~8 hrs/week -> total 16h vs 8h max
        )
        self.stdout.write(self.style.SUCCESS(f"  [+] Teacher Conflict 2: Overloaded {teacher_overload_demo.name} with 16h/wk (contract limit: {teacher_overload_demo.max_hours_per_week}h/wk)."))

        # -------------------------------------------------------------
        # 3. CLASSROOM CONFLICT A: Room Capacity Deficit
        # -------------------------------------------------------------
        # Create a specialized Lab division or assign large division to an undersized lab
        tiny_lab, _ = Room.objects.get_or_create(
            name="Micro-Robotics Lab 305 [Conflict Demo]",
            defaults={"is_lab": True, "capacity": 25},  # Only seats 25
        )
        sub_robotics_lab, _ = Subject.objects.get_or_create(
            name="Robotics Hardware Lab",
            defaults={"is_lab": True, "code": "CS803L"},
        )
        div_large = YearDivision.objects.filter(strength__gte=55).first()
        if div_large:
            assign_tiny_room, _ = Assignment.objects.get_or_create(
                semester=semester,
                teacher=teacher_unavail_demo,
                subject=sub_robotics_lab,
                division=div_large,  # Strength 55+ into capacity 25 lab
                defaults={"total_hours_for_semester": 45},
            )
            self.stdout.write(self.style.SUCCESS(f"  [+] Classroom Conflict 1: Assigned division {div_large.name} (Strength: {div_large.strength}) to {tiny_lab.name} (Capacity: {tiny_lab.capacity})."))

        # -------------------------------------------------------------
        # 4. Run the Solver & Diagnostic Issue Engine
        # -------------------------------------------------------------
        self.stdout.write(self.style.NOTICE("Running CP-SAT solver to demonstrate autonomous conflict handling..."))
        result = generate_timetable(semester.id)

        self.stdout.write(self.style.SUCCESS(f"\nSolver execution completed:"))
        self.stdout.write(f"  Total Scheduled Hours: {result['total_hours_scheduled']} / {result['total_hours_requested']}")
        self.stdout.write(f"  Issues / Conflicts Systematically Handled: {result['issues_count']}")
        self.stdout.write(f"  Message: {result['message']}\n")

        self.stdout.write(self.style.NOTICE("Sample Captured Conflict Records:"))
        for issue in SchedulingIssue.objects.filter(semester=semester)[:4]:
            self.stdout.write(self.style.WARNING(f"  * Issue on {issue.assignment}:"))
            self.stdout.write(f"    Reason: {issue.reason}")
            self.stdout.write(f"    Suggestion: {issue.suggestion}\n")
