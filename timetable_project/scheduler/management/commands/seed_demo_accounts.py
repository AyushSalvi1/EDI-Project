from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from scheduler.models import (
    Semester,
    Student,
    Teacher,
    TimetableEntry,
    YearDivision,
)
from scheduler.roles import ADMIN_GROUP, STUDENT_GROUP, TEACHER_GROUP


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

TEACHER_USERNAME = "teacher.demo"
TEACHER_PASSWORD = "Teach@12345"

STUDENT_USERNAME = "student.demo"
STUDENT_PASSWORD = "Study@12345"


class Command(BaseCommand):
    help = (
        "Create one admin, one teacher and one student demo login, each bound to a "
        "record that already has timetable classes, so every role can be demonstrated.\n\n"
        "Required setup order:\n"
        "  1. python manage.py migrate\n"
        "  2. python manage.py import_college_data sample_college_data.json\n"
        "  3. python manage.py generate_timetable\n"
        "  4. python manage.py seed_demo_accounts\n"
        "Running out of sequence will fail silently or confusingly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--semester-id",
            type=int,
            default=None,
            help="Bind demo accounts to classes in this semester. Defaults to the semester with the most classes.",
        )

    def _resolve_semester(self, semester_id):
        if semester_id:
            semester = Semester.objects.filter(pk=semester_id).first()
            if semester is None:
                raise CommandError(f"No semester found with id {semester_id}.")
            return semester

        return (
            Semester.objects.annotate(total=Count("timetable_entries"))
            .order_by("-total", "-start_date")
            .first()
            or Semester.objects.order_by("-start_date").first()
        )

    def _busiest(self, semester, field):
        """Return the id of the teacher/division with the most classes this semester."""
        row = (
            TimetableEntry.objects.filter(semester=semester)
            .values(field)
            .annotate(total=Count("id"))
            .order_by("-total")
            .first()
        )
        return row[field] if row else None

    def _seed_admin(self):
        Group.objects.get_or_create(name=ADMIN_GROUP)
        user, created = User.objects.get_or_create(
            username=ADMIN_USERNAME,
            defaults={"email": "admin@example.com"},
        )
        user.is_staff = True
        user.is_superuser = True
        user.set_password(ADMIN_PASSWORD)
        user.save()
        return user, created

    def _seed_teacher(self, semester):
        teacher_id = self._busiest(semester, "assignment__teacher_id")
        if teacher_id is None:
            return None, False
        teacher = Teacher.objects.filter(pk=teacher_id).first()
        if teacher is None:
            return None, False

        Group.objects.get_or_create(name=TEACHER_GROUP)
        user, created = User.objects.get_or_create(
            username=TEACHER_USERNAME,
            defaults={"first_name": teacher.name, "email": "teacher.demo@example.com"},
        )
        user.first_name = teacher.name
        user.is_staff = False
        user.is_superuser = False
        user.set_password(TEACHER_PASSWORD)
        user.save()
        user.groups.add(Group.objects.get(name=TEACHER_GROUP))

        # Teacher.user is one-to-one, so release any previous demo link first.
        Teacher.objects.exclude(pk=teacher.pk).filter(user=user).update(user=None)
        teacher.user = user
        teacher.save(update_fields=["user"])
        return teacher, created

    def _seed_student(self, semester):
        division_id = self._busiest(semester, "assignment__division_id")
        if division_id is None:
            return None, False
        division = YearDivision.objects.filter(pk=division_id).first()
        if division is None:
            return None, False

        Group.objects.get_or_create(name=STUDENT_GROUP)
        user, user_created = User.objects.get_or_create(
            username=STUDENT_USERNAME,
            defaults={"first_name": "Demo", "last_name": "Student", "email": "student.demo@example.com"},
        )
        user.first_name = "Demo"
        user.last_name = "Student"
        user.is_staff = False
        user.is_superuser = False
        user.set_password(STUDENT_PASSWORD)
        user.save()
        user.groups.add(Group.objects.get(name=STUDENT_GROUP))

        student = Student.objects.filter(user=user).first()
        created = user_created or student is None
        if student is None:
            roll_number = "DEMO-001"
            suffix = 1
            while Student.objects.filter(roll_number=roll_number).exists():
                suffix += 1
                roll_number = f"DEMO-{suffix:03d}"
            student = Student(user=user, roll_number=roll_number)

        student.full_name = "Demo Student"
        student.division = division
        student.is_active = True
        student.save()
        return student, created

    def handle(self, *args, **options):
        semester = self._resolve_semester(options.get("semester_id"))
        if semester is None:
            raise CommandError(
                "No semesters exist yet. Run 'python manage.py import_college_data "
                "sample_college_data.json' first."
            )

        _, admin_created = self._seed_admin()
        teacher, teacher_created = self._seed_teacher(semester)
        student, student_created = self._seed_student(semester)

        self.stdout.write(self.style.SUCCESS(f"\nDemo accounts ready for semester '{semester.name}'\n"))
        self.stdout.write(
            f"  {'CREATED' if admin_created else 'RESET  '}  {ADMIN_USERNAME:<13} / {ADMIN_PASSWORD}"
            f"  (superuser - dashboard, solver, Manage)"
        )
        if teacher:
            self.stdout.write(
                f"  {'CREATED' if teacher_created else 'RESET  '}  {TEACHER_USERNAME:<13} / {TEACHER_PASSWORD}"
                f"  (teacher portal - {teacher.name})"
            )
        else:
            self.stdout.write(self.style.WARNING("  SKIPPED   teacher demo - no teacher allocations in this semester"))
        if student:
            self.stdout.write(
                f"  {'CREATED' if student_created else 'RESET  '}  {STUDENT_USERNAME:<13} / {STUDENT_PASSWORD}"
                f"  (student portal - {student.division.name})"
            )
        else:
            self.stdout.write(self.style.WARNING("  SKIPPED   student demo - no divisions in this semester"))

        self.stdout.write(self.style.NOTICE("\n  Sign in at /login/. Change these passwords before any real deployment."))
        self.stdout.write("")