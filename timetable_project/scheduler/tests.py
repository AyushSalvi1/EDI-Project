import json
from collections import Counter
from datetime import time
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from reportlab.pdfgen import canvas as reportlab_canvas

User = get_user_model()

from scheduler.models import (
    YearDivision,
    Room,
    Teacher,
    TeacherUnavailability,
    Student,
    Assignment,
    TimetableEntry,
    TimetableChangeLog,
    SchedulingIssue,
    SolverRun,
    Semester,
    TimeSlot,
    generate_time_slots,
    Notification,
    ProposedChange,
    SchedulingPreference,
)
from scheduler.capacity import analyse_capacity
from scheduler.edits import delete_entry, move_entry, placement_conflicts, swap_entries
from scheduler.importer import import_college_data_from_dict
from scheduler.pdf_import import build_payload, classify_tables
from scheduler.recommendations import recommend_for_semester
from scheduler.solver import generate_timetable, TimetableSolverError

Canvas = reportlab_canvas.Canvas


class TimetableGeneratorTestCase(TestCase):

    def setUp(self):
        self.sample_data = {
            "college": {
                "name": "Test Engineering College",
                "semester_name": "Spring 2026",
                "start_date": "2026-01-05",
                "end_date": "2026-04-20",  # ~15 weeks: 105 days // 7 = 15
                "working_days": ["MON", "TUE", "WED", "THU", "FRI"],
                "daily_start_time": "09:00",
                "daily_end_time": "17:00",
                "period_duration_minutes": 60,
                "lunch_break": {"start_time": "13:00", "end_time": "14:00"}
            },
            "years": [
                {"year": 1, "number_of_divisions": 2, "strength_per_division": 60},
                {"year": 2, "number_of_divisions": 2, "strength_per_division": 60},
                {"year": 3, "number_of_divisions": 1, "strength_per_division": 50}
            ],
            "subjects": [
                {"id": "MATH1", "name": "Applied Mathematics", "is_lab": False},
                {"id": "PHY1", "name": "Physics", "is_lab": False},
                {"id": "PHYL1", "name": "Physics Lab", "is_lab": True},
                {"id": "CS1", "name": "Data Structures", "is_lab": False},
                {"id": "CSL1", "name": "Programming Lab", "is_lab": True}
            ],
            "rooms": {
                "number_of_regular_rooms": 10,
                "regular_room_capacity": 70,
                "number_of_lab_rooms": 4,
                "lab_room_capacity": 60
            },
            "teachers": [
                {
                    "id": "T1",
                    "name": "Dr. Raman",
                    "max_hours_per_week": 20,
                    "unavailable_slots": ["MON-P1"],
                    "allocations": [
                        {"year": 1, "division": 1, "subject_id": "PHY1", "total_hours_for_semester": 45},
                        {"year": 1, "division": 2, "subject_id": "PHY1", "total_hours_for_semester": 45},
                        {"year": 1, "division": 1, "subject_id": "PHYL1", "total_hours_for_semester": 30}
                    ]
                },
                {
                    "id": "T2",
                    "name": "Prof. Bose",
                    "max_hours_per_week": 20,
                    "unavailable_slots": ["FRI-P5"],
                    "allocations": [
                        {"year": 1, "division": 1, "subject_id": "MATH1", "total_hours_for_semester": 60},
                        {"year": 1, "division": 2, "subject_id": "MATH1", "total_hours_for_semester": 60},
                        {"year": 2, "division": 1, "subject_id": "MATH1", "total_hours_for_semester": 45}
                    ]
                },
                {
                    "id": "T3",
                    "name": "Dr. Sarabhai",
                    "max_hours_per_week": 20,
                    "unavailable_slots": [],
                    "allocations": [
                        {"year": 2, "division": 1, "subject_id": "CS1", "total_hours_for_semester": 45},
                        {"year": 2, "division": 2, "subject_id": "CS1", "total_hours_for_semester": 45},
                        {"year": 2, "division": 1, "subject_id": "CSL1", "total_hours_for_semester": 30},
                        {"year": 2, "division": 2, "subject_id": "CSL1", "total_hours_for_semester": 30}
                    ]
                },
                {
                    "id": "T4",
                    "name": "Prof. Kalam",
                    "max_hours_per_week": 16,
                    "unavailable_slots": ["WED-P3"],
                    "allocations": [
                        {"year": 3, "division": 1, "subject_id": "PHY1", "total_hours_for_semester": 45},
                        {"year": 3, "division": 1, "subject_id": "PHYL1", "total_hours_for_semester": 30}
                    ]
                }
            ]
        }

    def test_import_and_independent_solver_verification(self):
        """
        1. Imports sample dataset.
        2. Runs solver.
        3. Independently iterates TimetableEntry rows to verify:
           - No teacher appears twice in the same time slot
           - No division appears twice in the same time slot
           - No room appears twice in the same time slot
           - No lab subject is in a non-lab room or vice versa
           - Teacher unavailability is strictly respected
        """
        # 1. Import
        result = import_college_data_from_dict(self.sample_data)
        semester = result["semester"]
        self.assertEqual(semester.number_of_weeks(), 15)
        self.assertEqual(result["divisions_count"], 5)
        self.assertEqual(result["subjects_count"], 5)
        self.assertEqual(result["rooms_count"], 14)
        self.assertEqual(result["teachers_count"], 4)

        # 2. Run solver
        solve_result = generate_timetable(semester.id)
        self.assertTrue(solve_result["success"])
        self.assertGreater(solve_result["total_hours_scheduled"], 0)

        # 3. Independent verification of generated rows
        entries = list(
            TimetableEntry.objects.filter(semester=semester)
            .select_related("assignment__teacher", "assignment__division", "assignment__subject", "room", "time_slot")
        )
        self.assertEqual(len(entries), solve_result["total_hours_scheduled"])

        teacher_slot_pairs = []
        division_slot_pairs = []
        room_slot_pairs = []
        assignment_counts = Counter()

        teacher_unavail_set = set(
            TeacherUnavailability.objects.values_list("teacher_id", "time_slot_id")
        )

        for entry in entries:
            t_id = entry.assignment.teacher_id
            div_id = entry.assignment.division_id
            r_id = entry.room_id
            slot_id = entry.time_slot_id

            # Constraint: No teacher appears twice in the same time slot
            teacher_slot_pairs.append((t_id, slot_id))

            # Constraint: No division appears twice in the same time slot
            division_slot_pairs.append((div_id, slot_id))

            # Constraint: No room appears twice in the same time slot
            room_slot_pairs.append((r_id, slot_id))

            # Constraint: Room type matching
            if entry.assignment.subject.is_lab:
                self.assertTrue(entry.room.is_lab, f"Lab subject {entry.assignment.subject} placed in non-lab room {entry.room}")
            else:
                self.assertFalse(entry.room.is_lab, f"Theory subject {entry.assignment.subject} placed in lab room {entry.room}")

            # Constraint: Teacher unavailability respected
            self.assertNotIn(
                (t_id, slot_id),
                teacher_unavail_set,
                f"Teacher {entry.assignment.teacher.name} scheduled during unavailable slot {entry.time_slot}"
            )

            assignment_counts[entry.assignment_id] += 1

        # Check pair uniqueness
        self.assertEqual(len(teacher_slot_pairs), len(set(teacher_slot_pairs)), "Duplicate teacher in same slot found!")
        self.assertEqual(len(division_slot_pairs), len(set(division_slot_pairs)), "Duplicate division in same slot found!")
        self.assertEqual(len(room_slot_pairs), len(set(room_slot_pairs)), "Duplicate room in same slot found!")

        # Check scheduled hours <= weekly hours
        for a_id, count in assignment_counts.items():
            assignment = Assignment.objects.get(pk=a_id)
            self.assertLessEqual(count, assignment.weekly_hours())

    def test_overcommitted_scenario_reports_scheduling_issue(self):
        """
        Creates a deliberately over-committed scenario:
        - A teacher assigned way more weekly hours than available slots / max_hours_per_week.
        Verifies:
        - The solver still returns a result (does not crash or return nothing)
        - A SchedulingIssue row is created with a non-empty reason and suggestion
        """
        result = import_college_data_from_dict(self.sample_data)
        semester = result["semester"]

        # Deliberately over-commit teacher T1
        # Set max_hours_per_week to 4, but assign 20 hours requested per week
        teacher_1 = Teacher.objects.get(name="Dr. Raman")
        teacher_1.max_hours_per_week = 3
        teacher_1.save()

        # Update assignment hours to require many hours: 150 hours over 15 weeks = 10 hrs/wk
        first_assignment = Assignment.objects.filter(semester=semester, teacher=teacher_1).first()
        first_assignment.total_hours_for_semester = 150  # 10 hrs/week
        first_assignment.save()

        # Run solver
        solve_result = generate_timetable(semester.id)
        self.assertTrue(solve_result["success"])
        self.assertGreater(solve_result["total_hours_scheduled"], 0)
        self.assertGreater(solve_result["issues_count"], 0)

        # Verify SchedulingIssue rows
        issues = list(SchedulingIssue.objects.filter(semester=semester))
        self.assertGreaterEqual(len(issues), 1)

        for issue in issues:
            self.assertLess(issue.hours_scheduled, issue.hours_requested)
            self.assertTrue(bool(issue.reason.strip()), "SchedulingIssue reason must not be empty.")
            self.assertTrue(bool(issue.suggestion.strip()), "SchedulingIssue suggestion must not be empty.")

    def test_lunch_break_exclusion(self):
        """Verify that generate_time_slots strictly excludes periods overlapping lunch break."""
        slots = generate_time_slots(
            working_days=["MON"],
            daily_start_time="09:00",
            daily_end_time="18:00",
            period_duration_minutes=60,
            lunch_break={"start_time": "13:00", "end_time": "14:00"}
        )

        for s in slots:
            # Period should not start or end within 13:00 - 14:00
            s_min = s.start_time.hour * 60 + s.start_time.minute
            e_min = s.end_time.hour * 60 + s.end_time.minute
            self.assertFalse(
                max(s_min, 780) < min(e_min, 840),
                f"TimeSlot {s} overlaps lunch break (13:00 - 14:00)"
            )

        # Check that periods are sequentially numbered skipping lunch
        period_nums = [s.period_number for s in slots]
        self.assertEqual(period_nums, list(range(1, len(slots) + 1)))

    def test_missing_room_raises_solver_error(self):
        """Verify that when a required room type does not exist, TimetableSolverError is raised with a clear message."""
        # Create a small dataset where a lab subject is requested, but 0 lab rooms exist
        no_lab_data = dict(self.sample_data)
        no_lab_data["rooms"] = {
            "number_of_regular_rooms": 5,
            "regular_room_capacity": 60,
            "number_of_lab_rooms": 0,
            "lab_room_capacity": 0
        }
        result = import_college_data_from_dict(no_lab_data)
        sem = result["semester"]

        with self.assertRaises(TimetableSolverError) as ctx:
            generate_timetable(sem.id)

        self.assertIn("No lab room exists", str(ctx.exception))


class TimetableWebViewsTestCase(TestCase):

    def setUp(self):
        self.admin_user = User.objects.create_superuser("admin_test", "admin@test.com", "password123")
        self.client.login(username="admin_test", password="password123")

        self.sample_data = {
            "college": {
                "name": "Web Test College",
                "semester_name": "Web Semester",
                "start_date": "2026-08-01",
                "end_date": "2026-11-20",
                "working_days": ["MON", "TUE", "WED"],
                "daily_start_time": "09:00",
                "daily_end_time": "14:00",
                "period_duration_minutes": 60,
                "lunch_break": {"start_time": "12:00", "end_time": "13:00"}
            },
            "years": [
                {"year": 1, "number_of_divisions": 1, "strength_per_division": 50}
            ],
            "subjects": [
                {"id": "MATH", "name": "Mathematics", "is_lab": False}
            ],
            "rooms": {
                "number_of_regular_rooms": 2,
                "regular_room_capacity": 60,
                "number_of_lab_rooms": 1,
                "lab_room_capacity": 30
            },
            "teachers": [
                {
                    "id": "T1",
                    "name": "Prof. Web",
                    "max_hours_per_week": 10,
                    "unavailable_slots": [],
                    "allocations": [
                        {"year": 1, "division": 1, "subject_id": "MATH", "total_hours_for_semester": 30}
                    ]
                }
            ]
        }
        self.import_result = import_college_data_from_dict(self.sample_data)
        self.semester = self.import_result["semester"]

    def test_dashboard_view(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Automatic Timetable Generator")

    def test_application_views_require_authentication(self):
        self.client.logout()
        for path in ["/", "/upload/", "/generate/", "/timetable/", "/my-timetable/"]:
            response = self.client.get(path)
            self.assertRedirects(response, f"/login/?next={path}")

    def test_non_admin_cannot_reach_admin_pages(self):
        User.objects.create_user("limited_viewer", password="password123")

        self.client.logout()
        self.client.login(username="limited_viewer", password="password123")

        for path in ["/upload/", "/generate/", "/timetable/", "/manage/entries/"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/my-timetable/")

    def test_upload_json_view_get(self):
        response = self.client.get("/upload/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload College Schedule Data")

    def test_upload_json_view_post_valid(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile

        json_bytes = json.dumps(self.sample_data).encode("utf-8")
        uploaded = SimpleUploadedFile("test_college.json", json_bytes, content_type="application/json")

        response = self.client.post("/upload/", {"json_file": uploaded}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Successfully imported data")

    def test_upload_json_view_rejects_wrong_content_type_and_oversized_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        wrong_type = SimpleUploadedFile(
            "test_college.json", b"{}", content_type="text/plain"
        )
        response = self.client.post("/upload/", {"json_file": wrong_type})
        self.assertContains(response, "Invalid file type")

        oversized = SimpleUploadedFile(
            "test_college.json", b"x" * (5 * 1024 * 1024 + 1), content_type="application/json"
        )
        response = self.client.post("/upload/", {"json_file": oversized})
        self.assertContains(response, "too large")

    def test_generate_timetable_view(self):
        response = self.client.post("/generate/", {"semester_id": self.semester.id}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Successfully generated timetable")
        self.assertEqual(SolverRun.objects.filter(semester=self.semester).count(), 1)

    def test_timetable_view_grid(self):
        # Generate timetable first
        generate_timetable(self.semester.id)
        div = YearDivision.objects.first()
        response = self.client.get(f"/timetable/?semester_id={self.semester.id}&division_id={div.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, div.name)
        self.assertContains(response, "Period 1")


ROLE_TEST_DATA = {
    "college": {
        "name": "Portal Test College",
        "semester_name": "Portal Semester 2026",
        "start_date": "2026-01-05",
        "end_date": "2026-04-20",
        "working_days": ["MON", "TUE", "WED", "THU", "FRI"],
        "daily_start_time": "09:00",
        "daily_end_time": "17:00",
        "period_duration_minutes": 60,
        "lunch_break": {"start_time": "13:00", "end_time": "14:00"}
    },
    "years": [{"year": 1, "number_of_divisions": 2, "strength_per_division": 50}],
    "subjects": [
        {"id": "MATH", "name": "Mathematics", "is_lab": False},
        {"id": "PHY", "name": "Physics", "is_lab": False},
        {"id": "PHYL", "name": "Physics Lab", "is_lab": True},
    ],
    "rooms": {
        "number_of_regular_rooms": 3,
        "regular_room_capacity": 60,
        "number_of_lab_rooms": 1,
        "lab_room_capacity": 60,
    },
    "teachers": [
        {
            "id": "T1",
            "name": "Dr. Raman",
            "max_hours_per_week": 10,
            "unavailable_slots": [],
            "allocations": [
                {"year": 1, "division": 1, "subject_id": "MATH", "total_hours_for_semester": 30},
                {"year": 1, "division": 2, "subject_id": "MATH", "total_hours_for_semester": 30},
            ],
        },
        {
            "id": "T2",
            "name": "Prof. Bose",
            "max_hours_per_week": 10,
            "unavailable_slots": [],
            "allocations": [
                {"year": 1, "division": 1, "subject_id": "PHY", "total_hours_for_semester": 45},
                {"year": 1, "division": 1, "subject_id": "PHYL", "total_hours_for_semester": 30},
            ],
        },
    ],
}


class RoleBasedAccessTestCase(TestCase):
    """Students and teachers see only their own timetable; admins keep full control."""

    def setUp(self):
        # import a two-division dataset and solve it
        self.import_result = import_college_data_from_dict(ROLE_TEST_DATA)
        self.semester = self.import_result["semester"]
        generate_timetable(self.semester.id)

        self.div1 = YearDivision.objects.get(year=1, division_number=1)
        self.div2 = YearDivision.objects.get(year=1, division_number=2)
        self.teacher_raman = Teacher.objects.get(name="Dr. Raman")
        self.teacher_bose = Teacher.objects.get(name="Prof. Bose")

        self.admin_user = User.objects.create_user("portal_admin", password="password123")
        self.admin_user.is_staff = True
        self.admin_user.save()

        self.raman_user = User.objects.create_user("raman", password="password123")
        self.teacher_raman.user = self.raman_user
        self.teacher_raman.save()

        self.bose_user = User.objects.create_user("bose", password="password123")
        self.teacher_bose.user = self.bose_user
        self.teacher_bose.save()

        self.student_div1_user = User.objects.create_user("stu_div1", password="password123")
        Student.objects.create(
            user=self.student_div1_user,
            roll_number="R001",
            full_name="Asha Student",
            division=self.div1,
        )

        self.student_div2_user = User.objects.create_user("stu_div2", password="password123")
        Student.objects.create(
            user=self.student_div2_user,
            roll_number="R002",
            full_name="Bilal Student",
            division=self.div2,
        )

    # ---------------------------------------------------------------- roles
    def test_teacher_sees_only_own_classes(self):
        self.client.login(username="bose", password="password123")
        response = self.client.get("/my-timetable/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "teacher")

        expected = TimetableEntry.objects.filter(
            semester=self.semester, assignment__teacher=self.teacher_bose
        ).count()
        self.assertEqual(len(response.context["entries"]), expected)
        self.assertGreater(expected, 0)

        # Prof. Bose only teaches 1st Year Division 1, so no other division leaks in.
        self.assertNotContains(response, self.div2.name)
        self.assertContains(response, self.div1.name)

    def test_teacher_spanning_two_divisions_sees_both(self):
        self.client.login(username="raman", password="password123")
        response = self.client.get("/my-timetable/")

        self.assertEqual(response.status_code, 200)
        expected = TimetableEntry.objects.filter(
            semester=self.semester, assignment__teacher=self.teacher_raman
        ).count()
        self.assertEqual(len(response.context["entries"]), expected)
        self.assertContains(response, self.div1.name)
        self.assertContains(response, self.div2.name)

    def test_student_sees_only_own_division(self):
        self.client.login(username="stu_div1", password="password123")
        response = self.client.get("/my-timetable/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "student")

        expected = TimetableEntry.objects.filter(
            semester=self.semester, assignment__division=self.div1
        ).count()
        self.assertEqual(len(response.context["entries"]), expected)
        self.assertContains(response, "Asha Student")
        self.assertContains(response, self.div1.name)
        self.assertNotContains(response, self.div2.name)

    def test_student_could_not_pick_another_division_via_query_string(self):
        self.client.login(username="stu_div2", password="password123")
        response = self.client.get(f"/my-timetable/?division_id={self.div1.id}")

        self.assertEqual(response.status_code, 200)
        expected = TimetableEntry.objects.filter(
            semester=self.semester, assignment__division=self.div2
        ).count()
        self.assertEqual(len(response.context["entries"]), expected)
        self.assertNotContains(response, self.div1.name)

    def test_non_admins_are_redirected_away_from_admin_pages(self):
        for username in ["bose", "stu_div1"]:
            with self.subTest(account=username):
                self.client.logout()
                self.client.login(username=username, password="password123")
                for path in ["/upload/", "/generate/", "/timetable/", "/manage/entries/", "/manage/swap/"]:
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.url, "/my-timetable/")

    def test_root_redirects_non_admins_to_my_timetable(self):
        for username in ["bose", "stu_div1"]:
            with self.subTest(account=username):
                self.client.logout()
                self.client.login(username=username, password="password123")
                response = self.client.get("/")
                self.assertRedirects(response, "/my-timetable/")

    def test_admin_keeps_full_dashboard_and_manager_access(self):
        self.client.login(username="portal_admin", password="password123")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/timetable/").status_code, 200)
        self.assertEqual(self.client.get("/manage/entries/").status_code, 200)
        self.assertEqual(self.client.get("/manage/swap/").status_code, 200)

    def test_staff_sees_admin_navigation_not_limited_navigation(self):
        self.client.login(username="portal_admin", password="password123")
        response = self.client.get("/")
        self.assertContains(response, "/upload/pdf/")
        self.assertContains(response, "Manage")

        self.client.logout()
        self.client.login(username="stu_div1", password="password123")
        response = self.client.get("/my-timetable/")
        self.assertContains(response, "My Timetable")
        self.assertNotContains(response, "/upload/pdf/")

    # ------------------------------------------------------------ auth flow
    def test_login_page_is_public_and_successful_login_routes_by_role(self):
        self.assertEqual(self.client.get("/login/").status_code, 200)

        self.client.post("/login/", {"username": "stu_div1", "password": "password123"})
        self.assertEqual(self.client.get("/").url, "/my-timetable/")

        self.client.logout()
        self.client.post("/login/", {"username": "portal_admin", "password": "password123"})
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_rejects_bad_credentials(self):
        response = self.client.post("/login/", {"username": "stu_div1", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not recognised")

    def test_logout_requires_post(self):
        self.client.login(username="stu_div1", password="password123")
        self.assertEqual(self.client.get("/logout/").status_code, 200)

        response = self.client.post("/logout/")
        self.assertRedirects(response, "/login/")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_student_registration_creates_account_scoped_to_division(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Chandra Student",
                "roll_number": "R003",
                "email": "chandra@example.com",
                "division_id": self.div2.id,
                "username": "chandra",
                "password1": "portalpass123",
                "password2": "portalpass123",
            },
        )
        self.assertRedirects(response, "/my-timetable/")

        student = Student.objects.get(roll_number="R003")
        self.assertEqual(student.division, self.div2)
        self.assertEqual(student.full_name, "Chandra Student")
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_student_registration_rejects_duplicate_roll_number(self):
        response = self.client.post(
            "/register/",
            {
                "full_name": "Duplicate Roll",
                "roll_number": "R001",
                "division_id": self.div1.id,
                "username": "dupe_roll",
                "password1": "portalpass123",
                "password2": "portalpass123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already registered")
        self.assertFalse(User.objects.filter(username="dupe_roll").exists())


class ManualTimetableEditTestCase(TestCase):
    """Administrator manual edits must never introduce a conflict."""

    def setUp(self):
        self.import_result = import_college_data_from_dict(ROLE_TEST_DATA)
        self.semester = self.import_result["semester"]
        generate_timetable(self.semester.id)

        self.admin_user = User.objects.create_user("edit_admin", password="password123")
        self.admin_user.is_staff = True
        self.admin_user.save()

        self.entries = list(
            TimetableEntry.objects.filter(semester=self.semester).select_related(
                "assignment__subject", "assignment__teacher", "assignment__division",
                "room", "time_slot",
            )
        )

    def _free_spot_for(self, entry):
        """Find a *different* (slot, room) pair with no rule violation for this entry."""
        for slot in TimeSlot.objects.all():
            for room in Room.objects.all():
                if slot.id == entry.time_slot_id and room.id == entry.room_id:
                    continue
                if not placement_conflicts(entry.assignment, slot, room, [entry.pk]):
                    return slot, room
        return None, None

    def test_move_entry_to_a_free_slot_succeeds_and_is_logged(self):
        entry = self.entries[0]
        slot, room = self._free_spot_for(entry)
        self.assertIsNotNone(slot, "Expected at least one conflict-free slot/room pair")

        original_slot, original_room = entry.time_slot_id, entry.room_id
        moved, conflicts = move_entry(entry, slot, room, user=self.admin_user)

        self.assertEqual(conflicts, [])
        moved.refresh_from_db()
        self.assertEqual(moved.time_slot_id, slot.id)
        self.assertEqual(moved.room_id, room.id)
        self.assertNotEqual((moved.time_slot_id, moved.room_id), (original_slot, original_room))

        log = TimetableChangeLog.objects.filter(action="MOVE").latest("created_at")
        self.assertIsNotNone(log)
        self.assertEqual(log.changed_by, self.admin_user)

    def test_move_entry_into_an_occupied_room_is_rejected(self):
        first, second = self.entries[0], self.entries[1]

        # Try to drop the first class exactly where the second one already sits.
        blocked_slot, blocked_room = second.time_slot, second.room
        _, conflicts = move_entry(first, blocked_slot, blocked_room, user=self.admin_user)

        self.assertTrue(conflicts)
        self.assertTrue(any("already occupied" in c for c in conflicts))
        first.refresh_from_db()
        self.assertNotEqual(
            (first.time_slot_id, first.room_id),
            (blocked_slot.id, blocked_room.id),
            "Entry must not have moved when the move was rejected",
        )
        self.assertFalse(TimetableChangeLog.objects.filter(action="MOVE").exists())

    def test_swap_is_rejected_when_teacher_is_unavailable_in_the_target_period(self):
        """
        Moving an entry into a period its teacher cannot teach must be refused,
        even though the room and the division are free.
        """
        first, second = self.entries[0], self.entries[1]
        self.assertNotEqual(first.time_slot_id, second.time_slot_id)

        blocked_slot = second.time_slot
        TeacherUnavailability.objects.create(
            teacher=first.assignment.teacher, time_slot=blocked_slot
        )

        ok, conflicts = swap_entries(first, second, user=self.admin_user)

        self.assertFalse(ok)
        self.assertTrue(any("unavailable" in c.lower() for c in conflicts))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.time_slot_id, self.entries[0].time_slot_id)
        self.assertEqual(second.time_slot_id, self.entries[1].time_slot_id)
        self.assertFalse(TimetableChangeLog.objects.filter(action="SWAP").exists())

    def test_swapping_a_teachers_own_two_classes_is_allowed(self):
        """
        Exchanging the periods of two classes belonging to the same teacher is
        legal: that teacher still holds exactly one class in each period.
        """
        by_teacher = {}
        for entry in self.entries:
            by_teacher.setdefault(entry.assignment.teacher_id, []).append(entry)

        legal_pair = None
        for group in by_teacher.values():
            for i, first in enumerate(group):
                for second in group[i + 1:]:
                    if first.time_slot_id == second.time_slot_id:
                        continue
                    excluded = [first.pk, second.pk]
                    if placement_conflicts(first.assignment, second.time_slot, first.room, excluded):
                        continue
                    if placement_conflicts(second.assignment, first.time_slot, second.room, excluded):
                        continue
                    legal_pair = (first, second)
                    break
                if legal_pair:
                    break
            if legal_pair:
                break
        self.assertIsNotNone(
            legal_pair, "Expected a teacher's own two classes to be swappable somewhere"
        )

        first, second = legal_pair
        first_slot, second_slot = first.time_slot_id, second.time_slot_id

        ok, conflicts = swap_entries(first, second, user=self.admin_user)

        self.assertTrue(ok, conflicts)
        refreshed = {
            e.pk: e.time_slot_id
            for e in TimetableEntry.objects.filter(pk__in=[first.pk, second.pk])
        }
        self.assertEqual(refreshed[first.pk], second_slot)
        self.assertEqual(refreshed[second.pk], first_slot)

    def test_move_entry_onto_unavailable_period_is_rejected(self):
        entry = next(e for e in self.entries if e.assignment.teacher.name == "Dr. Raman")
        empty_slot = next(
            s for s in TimeSlot.objects.all()
            if not TimetableEntry.objects.filter(time_slot=s).exists()
        )
        TeacherUnavailability.objects.create(teacher=entry.assignment.teacher, time_slot=empty_slot)

        conflicts = placement_conflicts(entry.assignment, empty_slot, entry.room, [entry.pk])
        self.assertTrue(any("unavailable" in c.lower() for c in conflicts))

        _, move_conflicts = move_entry(entry, empty_slot, entry.room, user=self.admin_user)
        self.assertTrue(move_conflicts)

    def test_placement_conflicts_rejects_room_type_mismatch(self):
        entry = next(e for e in self.entries if not e.assignment.subject.is_lab)
        lab_room = next(r for r in Room.objects.all() if r.is_lab)
        conflicts = placement_conflicts(entry.assignment, entry.time_slot, lab_room, [entry.pk])
        self.assertTrue(any("wrong room type" in c for c in conflicts))

    def test_placement_conflicts_rejects_undersized_room(self):
        entry = self.entries[0]
        tiny_room = Room.objects.create(name="Tiny Room", is_lab=entry.room.is_lab, capacity=1)
        conflicts = placement_conflicts(entry.assignment, entry.time_slot, tiny_room, [entry.pk])
        self.assertTrue(any("seats 1" in c for c in conflicts))

    def test_swap_entries_exchanges_periods_when_both_positions_are_legal(self):
        legal_pair = None
        for i, first in enumerate(self.entries):
            for second in self.entries[i + 1:]:
                if first.time_slot_id == second.time_slot_id:
                    continue
                excluded = [first.pk, second.pk]
                if placement_conflicts(first.assignment, second.time_slot, first.room, excluded):
                    continue
                if placement_conflicts(second.assignment, first.time_slot, second.room, excluded):
                    continue
                legal_pair = (first, second)
                break
            if legal_pair:
                break
        self.assertIsNotNone(legal_pair, "No conflict-free swap pair available in the generated timetable")

        first, second = legal_pair
        first_slot, second_slot = first.time_slot_id, second.time_slot_id

        ok, conflicts = swap_entries(first, second, user=self.admin_user)
        self.assertTrue(ok, conflicts)

        refreshed = {
            e.pk: e for e in TimetableEntry.objects.filter(
                assignment_id__in=[first.assignment_id, second.assignment_id]
            ).select_related("time_slot")
        }
        self.assertEqual(refreshed[first.pk].time_slot_id, second_slot)
        self.assertEqual(refreshed[second.pk].time_slot_id, first_slot)

        log = TimetableChangeLog.objects.filter(action="SWAP").latest("created_at")
        self.assertIsNotNone(log)

    def test_swap_rejects_selecting_the_same_entry_twice(self):
        entry = self.entries[0]
        ok, conflicts = swap_entries(entry, entry, user=self.admin_user)
        self.assertFalse(ok)
        self.assertTrue(conflicts)

    def test_delete_entry_removes_row_and_logs(self):
        entry = self.entries[0]
        pk = entry.pk
        delete_entry(entry, user=self.admin_user)

        self.assertFalse(TimetableEntry.objects.filter(pk=pk).exists())
        log = TimetableChangeLog.objects.filter(action="DELETE").latest("created_at")
        self.assertIsNotNone(log)
        self.assertEqual(log.changed_by, self.admin_user)

    def test_manager_view_delete_flow(self):
        entry = self.entries[0]
        pk = entry.pk

        self.client.login(username="edit_admin", password="password123")

        confirm = self.client.get(f"/manage/entries/{pk}/delete/")
        self.assertEqual(confirm.status_code, 200)

        response = self.client.post(f"/manage/entries/{pk}/delete/", follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TimetableEntry.objects.filter(pk=pk).exists())
        self.assertContains(response, "Removed")

    def test_manager_view_filters_by_division(self):
        self.client.login(username="edit_admin", password="password123")
        division = YearDivision.objects.first()
        response = self.client.get(
            f"/manage/entries/?semester_id={self.semester.id}&division_id={division.id}"
        )
        self.assertEqual(response.status_code, 200)
        for entry in response.context["page"]:
            self.assertEqual(entry.assignment.division_id, division.id)



class PdfImportParsingTestCase(TestCase):
    """Column detection, calendar aliases and payload building are pure text work."""

    def test_detects_tables_by_role_and_maps_columns_in_any_order(self):
        tables = [
            {
                "index": 0,
                "page": 1,
                "headers": ["Room No.", "Seat Capacity", "Room Type"],
                "rows": [["LH-101", "70", "Classroom"], ["LAB-A", "60", "Lab"]],
            },
            {
                "index": 1,
                "page": 1,
                "headers": ["Yr", "Faculty Name", "Div", "Subject", "Total Hours"],
                "rows": [["2", "Dr. Rao", "1", "Data Structures", "4"]],
            },
            {
                "index": 2,
                "page": 2,
                "headers": ["Academic Session", "Autumn Semester 2026"],
                "rows": [
                    ["Semester Dates", "03/08/2026 to 20/11/2026"],
                    ["Working Days", "Monday to Friday"],
                    ["College Timings", "09:00 am to 05:00 pm"],
                    ["Lunch Break", "1:00 pm to 2:00 pm"],
                ],
            },
]
        parsed = classify_tables(tables)

        roles = {t["role"]: t for t in parsed}
        self.assertEqual(set(roles), {"rooms", "allocations", "calendar"})

        # Columns are matched by header text, not by position.
        room_mapping = roles["rooms"]["mapping"]
        self.assertEqual(room_mapping["room_name"], 0)
        self.assertEqual(room_mapping["capacity"], 1)
        self.assertEqual(room_mapping["is_lab"], 2)

        alloc_mapping = roles["allocations"]["mapping"]
        self.assertEqual(alloc_mapping["teacher"], 1)
        self.assertEqual(alloc_mapping["subject_name"], 3)
        self.assertEqual(alloc_mapping["year"], 0)
        self.assertEqual(alloc_mapping["division"], 2)
        self.assertEqual(alloc_mapping["total_hours"], 4)

    def test_rejects_a_table_with_no_usable_columns(self):
        parsed = classify_tables([
            {"index": 0, "page": 1, "headers": ["Foo", "Bar"], "rows": [["1", "2"]]}
        ])
        self.assertEqual(parsed[0]["role"], "unknown")
        self.assertEqual(parsed[0]["mapping_confidence"], 0)

    def test_calendar_is_read_with_twelve_hour_and_phrase_aliases(self):
        tables = classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Academic Session", "Autumn Semester 2026"],
                "rows": [
                    ["Semester commences on", "03 August 2026"],
                    ["Semester concludes on", "20 November 2026"],
                    ["Working Days", "Monday to Friday"],
                    ["College Timings", "09:00 am to 05:00 pm"],
                    ["Lunch Break", "1:00 pm to 2:00 pm"],
                    ["Period Duration", "60 minutes"],
                ],
            },
            {
                "index": 1,
                "page": 1,
                "headers": ["Faculty Name", "Subject", "Yr", "Div", "Total Hours"],
                "rows": [["Dr. Rao", "Data Structures", "2", "1", "4"]],
            },
        ])
        payload = build_payload(tables)
        college = payload["college"]

        self.assertEqual(college["semester_name"], "Autumn Semester 2026")
        self.assertEqual(college["start_date"], "2026-08-03")
        self.assertEqual(college["end_date"], "2026-11-20")
        self.assertEqual(college["working_days"], ["MON", "TUE", "WED", "THU", "FRI"])
        self.assertEqual(college["daily_start_time"], "09:00")
        self.assertEqual(college["daily_end_time"], "17:00")
        self.assertEqual(college["lunch_break"], {"start_time": "13:00", "end_time": "14:00"})
        self.assertEqual(college["period_duration_minutes"], 60)

    def test_table_spanning_two_pages_is_rejoined(self):
        tables = classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Faculty Name", "Subject", "Yr", "Div"],
                "rows": [["Dr. Rao", "Data Structures", "2", "1"]],
                "continued": True,
            },
            {
                "index": 1,
                "page": 2,
                "headers": ["Faculty Name", "Subject", "Yr", "Div"],
                "rows": [["Dr. Iyer", "Operating Systems", "3", "2"]],
                "continues_table": 0,
            },
        ])
        roles = {t["role"]: t for t in tables}
        self.assertEqual(len(roles["allocations"]["rows"]), 2)
        self.assertIn("Dr. Iyer", roles["allocations"]["rows"][1])

    def test_years_and_divisions_come_from_the_allocation_grid(self):
        """Divisions are derived from the allocation rows, which are authoritative."""
        tables = classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Faculty Name", "Subject", "Yr", "Div", "Total Hours", "Strength"],
                "rows": [
                    ["Dr. Rao", "Data Structures", "1", "1", "4", "66"],
                    ["Dr. Rao", "Data Structures", "1", "2", "4", "66"],
                    ["Dr. Iyer", "Operating Systems", "2", "1", "3", "62"],
                ],
            },
        ])
        payload = build_payload(tables)

        self.assertEqual(
            payload["years"],
            [
                {"year": 1, "divisions": [
                    {"division_number": 1, "strength": 66},
                    {"division_number": 2, "strength": 66},
                ], "strength_per_division": 66},
                {"year": 2, "divisions": [
                    {"division_number": 1, "strength": 62},
                ], "strength_per_division": 62},
            ],
        )
        # Allocations are grouped under the teacher they belong to.
        by_name = {t["name"]: t for t in payload["teachers"]}
        self.assertEqual(len(by_name["Dr. Rao"]["allocations"]), 2)
        self.assertEqual(len(by_name["Dr. Iyer"]["allocations"]), 1)
        self.assertEqual(len(payload["teachers"]), 2)

    def test_year_outside_one_to_four_is_skipped_with_a_warning(self):
        tables = classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Faculty Name", "Subject", "Yr", "Div", "Total Hours"],
                "rows": [
                    ["Dr. Rao", "Data Structures", "1", "1", "4"],
                    ["Dr. Iyer", "Open Elective", "5", "1", "3"],
                ],
            },
        ])
        payload = build_payload(tables)

        self.assertEqual([y["year"] for y in payload["years"]], [1])
        self.assertTrue(
            any("Year 5" in w for w in payload.get("_warnings", [])),
            payload.get("_warnings"),
        )

    def test_oversized_division_strength_raises_a_capacity_warning(self):
        tables = classify_tables([
            {
                "index": 0,
                "page": 2,
                "headers": ["Room No.", "Seat Capacity", "Room Type"],
                "rows": [["LH-101", "70", "Classroom"], ["LAB-A", "60", "Lab"]],
            },
            {
                "index": 1,
                "page": 1,
                "headers": ["Yr", "Faculty Name", "Div", "Subject", "Total Hours", "Nature"],
                "rows": [["1", "Dr. Rao", "1", "Engineering Physics Lab", "2", "Lab"]],
            },
            {
                "index": 2,
                "page": 1,
                "headers": ["Academic Session", "Autumn Semester 2026"],
                "rows": [["Working Days", "Monday to Friday"]],
            },
        ])
        payload = build_payload(tables, overrides={"default_strength": 66})
        warnings = " ".join(payload.get("_warnings", []))

        self.assertIn("66", warnings)
        self.assertIn("lab", warnings.lower())

    def test_missing_calendar_table_is_a_hard_error(self):
        tables = classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Room No.", "Seat Capacity", "Room Type"],
                "rows": [["LH-101", "70", "Classroom"]],
            },
        ])
        with self.assertRaises(ValueError):
            build_payload(tables)


class PdfImportEndToEndTestCase(TestCase):
    """A recognised PDF must import through the same validated path as JSON."""

    @staticmethod
    def _tables():
        return classify_tables([
            {
                "index": 0,
                "page": 1,
                "headers": ["Academic Session", "Autumn Semester 2026"],
                "rows": [
                    ["Semester Dates", "03/08/2026 to 20/11/2026"],
                    ["Working Days", "Monday to Friday"],
                    ["College Timings", "09:00 am to 05:00 pm"],
                    ["Lunch Break", "1:00 pm to 2:00 pm"],
                    ["Period Duration", "60 minutes"],
                ],
            },
            {
                "index": 1,
                "page": 2,
                "headers": ["Room No.", "Seat Capacity", "Room Type"],
                "rows": [["LH-101", "70", "Classroom"], ["LAB-A", "60", "Lab"]],
            },
            {
                "index": 2,
                "page": 1,
                "headers": ["Faculty Name", "Subject", "Yr", "Div", "Total Hours",
                            "Max hrs/week", "Nature", "Strength"],
                "rows": [
                    # "Total Hours" is a semester total, so 45 over a 15-week
                    # semester is 3 periods a week.
                    ["Dr. Meera Krishnan", "Data Structures", "1", "1", "45", "18", "Theory", "60"],
                    ["Dr. Meera Krishnan", "Data Structures", "1", "2", "45", "18", "Theory", "60"],
                    ["Prof. Anil Deshpande", "Operating Systems Lab", "2", "1", "45", "20", "Lab", "60"],
                    ["Prof. Anil Deshpande", "Operating Systems Lab", "2", "2", "45", "20", "Lab", "60"],
                ],
            },
        ])

    @staticmethod
    def _payload():
        payload = build_payload(PdfImportEndToEndTestCase._tables())
        payload["college"]["name"] = "Test Institute of Technology"
        return payload

    def test_payload_matches_the_imported_shape(self):
        payload = self._payload()
        self.assertEqual(payload["college"]["semester_name"], "Autumn Semester 2026")
        self.assertEqual(payload["college"]["working_days"],
                         ["MON", "TUE", "WED", "THU", "FRI"])
        self.assertEqual(payload["college"]["daily_end_time"], "17:00")
        self.assertEqual(payload["college"]["lunch_break"],
                         {"start_time": "13:00", "end_time": "14:00"})
        self.assertEqual(payload["rooms"]["number_of_regular_rooms"], 1)
        self.assertEqual(payload["rooms"]["number_of_lab_rooms"], 1)
        self.assertEqual(payload["rooms"]["lab_room_capacity"], 60)

    def test_import_then_generate_produces_a_conflict_free_week(self):
        result = import_college_data_from_dict(self._payload())
        self.assertEqual(result["divisions_count"], 4)
        self.assertEqual(result["rooms_count"], 2)
        self.assertEqual(result["assignments_count"], 4)
        self.assertEqual(result["warnings"], [])

        summary = generate_timetable(result["semester"].id)

        self.assertTrue(summary["success"], summary["message"])
        self.assertEqual(
            summary["total_hours_scheduled"], summary["total_hours_requested"]
        )
        self.assertEqual(summary["issues_count"], 0)

        entries = TimetableEntry.objects.filter(semester=result["semester"])
        # Four offerings at three periods a week each.
        self.assertEqual(entries.count(), 12)
        self.assertEqual(summary["total_hours_scheduled"], 12)

        # No teacher, division or room may be double-booked: each owner may appear at
        # most once per day-period.
        for label, values in (
            ("teacher", entries.values_list("assignment__teacher_id", "time_slot__day",
                                            "time_slot_id")),
            ("division", entries.values_list("assignment__division_id", "time_slot__day",
                                             "time_slot_id")),
            ("room", entries.values_list("room_id", "time_slot__day", "time_slot_id")),
        ):
            self.assertEqual(len(values), len(set(values)), f"{label} double-booked")

        # Labs must land in labs, and every room must seat the whole division.
        for entry in entries.select_related("room", "assignment__division",
                                            "assignment__subject"):
            self.assertEqual(entry.room.is_lab, entry.assignment.subject.is_lab)
            self.assertGreaterEqual(
                entry.room.capacity, entry.assignment.division.strength
            )


class PdfExportTestCase(TestCase):
    """PDF exports must return a real, non-empty PDF for every portal."""

    @classmethod
    def setUpTestData(cls):
        payload = PdfImportEndToEndTestCase()._payload()
        cls.semester = import_college_data_from_dict(payload)["semester"]
        generate_timetable(cls.semester.id)
        cls.admin = User.objects.create_superuser(
            username="pdfadmin", email="pdf@example.com", password="pdf-admin-pw-123"
        )

    def test_admin_can_export_every_pdf_report(self):
        self.client.force_login(self.admin)
        division = YearDivision.objects.first()
        urls = [
            f"/export/division/{division.id}.pdf?semester_id={self.semester.id}",
            f"/export/full.pdf?semester_id={self.semester.id}",
            f"/export/faculty.pdf?semester_id={self.semester.id}",
            f"/export/issues.pdf?semester_id={self.semester.id}",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertTrue(response.content.startswith(b"%PDF-"))
                self.assertGreater(len(response.content), 1000)

    def test_non_admin_cannot_export(self):
        student = User.objects.create_user(username="pdfstudent", password="pdf-stu-pw-123")
        self.client.force_login(student)
        response = self.client.get(f"/export/full.pdf?semester_id={self.semester.id}")
        self.assertIn(response.status_code, (302, 403))


class PdfImportViewTestCase(TestCase):
    """The two-stage upload screen must enforce access control and reject junk."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="uploader", email="up@example.com", password="upload-pw-123"
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get("/upload/pdf/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_upload_screen_renders_for_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get("/upload/pdf/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import College Data from PDF")

    def test_non_pdf_upload_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/upload/pdf/",
            {"stage": "extract", "pdf_file": SimpleUploadedFile("data.csv", b"a,b\n1,2\n")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid file type")

    def test_pdf_without_tables_reports_a_clear_error(self):
        self.client.force_login(self.admin)
        pdf_bytes = _build_text_pdf("Just a plain paragraph of text with no table at all.")
        response = self.client.post(
            "/upload/pdf/",
            {"stage": "extract",
             "pdf_file": SimpleUploadedFile("blank.pdf", pdf_bytes, content_type="application/pdf")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No tables were found")

    def test_real_pdf_is_parsed_and_offered_for_mapping(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/upload/pdf/",
            {"stage": "extract",
             "pdf_file": SimpleUploadedFile(
                 "timetable.pdf", _build_sample_pdf(), content_type="application/pdf")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Detected Tables")
        self.assertContains(response, "Autumn Semester 2026")
        # The recognised tables must survive the round trip through the form.
        tables = json.loads(response.context["tables_json"])
        self.assertEqual({t["role"] for t in tables}, {"calendar", "rooms", "allocations"})

    def test_import_stage_persists_the_recognised_data(self):
        self.client.force_login(self.admin)
        extract = self.client.post(
            "/upload/pdf/",
            {"stage": "extract",
             "pdf_file": SimpleUploadedFile(
                 "timetable.pdf", _build_sample_pdf(), content_type="application/pdf")},
        )
        tables = json.loads(extract.context["tables_json"])
        response = self.client.post(
            "/upload/pdf/",
            {"stage": "import", "tables_json": json.dumps(tables), "period_duration_minutes": "60"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Semester.objects.count(), 1)
        self.assertEqual(Room.objects.count(), 2)
        self.assertEqual(Teacher.objects.count(), 2)


def _build_text_pdf(text):
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.setFont("Helvetica", 11)
    text_obj = canvas.beginText(40, 780)
    for line in text.splitlines():
        text_obj.textLine(line)
    canvas.drawText(text_obj)
    canvas.save()
    return buffer.getvalue()


def _build_sample_pdf():
    """A PDF shaped like a real college timetable, drawn with ruled cells."""
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.setFont("Helvetica", 9)

    def cell(x, y, w, h, text, bold=False):
        canvas.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        canvas.rect(x, y, w, h)
        canvas.drawString(x + 3, y + h / 2 - 3, text[:24])

    y = 760
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(40, y, "Sardar Vallabhbhai Patel Institute of Technology")
    y -= 26

    # Calendar key/value table.
    calendar_rows = [
        ("Academic Session", "Autumn Semester 2026"),
        ("Semester Dates", "03/08/2026 to 20/11/2026"),
        ("Working Days", "Monday to Friday"),
        ("College Timings", "09:00 am to 05:00 pm"),
        ("Lunch Break", "1:00 pm to 2:00 pm"),
    ]
    for left, right in calendar_rows:
        cell(40, y, 110, 20, left, bold=True)
        cell(150, y, 200, 20, right)
        y -= 20
    y -= 14

    # Room table.
    cell(40, y, 80, 20, "Room No.", bold=True)
    cell(120, y, 80, 20, "Seat Capacity", bold=True)
    cell(200, y, 80, 20, "Room Type", bold=True)
    y -= 20
    for name, capacity, kind in (("LH-101", "70", "Classroom"), ("LAB-A", "60", "Lab")):
        cell(40, y, 80, 20, name)
        cell(120, y, 80, 20, capacity)
        cell(200, y, 80, 20, kind)
        y -= 20
    y -= 14

    # Allocation table.
    headers = ("Faculty Name", "Subject", "Yr", "Div", "Total Hours", "Nature", "Strength")
    widths = (110, 100, 30, 30, 55, 50, 50)
    x = 40
    for header, width in zip(headers, widths):
        cell(x, y, width, 20, header, bold=True)
        x += width
    y -= 20
    allocation_rows = (
        ("Dr. Meera Krishnan", "Data Structures", "1", "1", "3", "Theory", "60"),
        ("Dr. Meera Krishnan", "Data Structures", "1", "2", "3", "Theory", "60"),
        ("Prof. Anil Deshpande", "Operating Systems Lab", "2", "1", "3", "Lab", "60"),
        ("Prof. Anil Deshpande", "Operating Systems Lab", "2", "2", "3", "Lab", "60"),
    )
    for row in allocation_rows:
        x = 40
        for value, width in zip(row, widths):
            cell(x, y, width, 20, value)
            x += width
        y -= 20

    canvas.save()
    return buffer.getvalue()


class CapacityReportTestCase(TestCase):
    """The pre-solve report must name the missing infrastructure, not fail."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="capacityadmin", email="cap@example.com", password="capacity-pw-123"
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get("/capacity/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_empty_college_reports_no_data_rather_than_crashing(self):
        self.client.force_login(self.admin)
        response = self.client.get("/capacity/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["plan"])

    def test_oversized_division_is_reported_as_blocking(self):
        # One 70-student division but the only laboratory seats 30.
        payload = build_payload(PdfImportEndToEndTestCase._tables())
        payload["college"]["name"] = "Capacity Institute"
        payload["rooms"]["lab_room_capacity"] = 30
        payload["rooms"]["number_of_lab_rooms"] = 1
        semester = import_college_data_from_dict(payload)["semester"]

        plan = analyse_capacity(semester)

        self.assertEqual(plan["verdict"], "unschedulable")
        self.assertTrue(plan["blocking_findings"])
        self.assertTrue(
            any("laborator" in f["message"].lower() for f in plan["blocking_findings"]),
            plan["blocking_findings"],
        )

        self.client.force_login(self.admin)
        response = self.client.get(f"/capacity/?semester_id={semester.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be fully scheduled")
        self.assertContains(response, "Division Strength Coverage")

    def test_adequate_infrastructure_is_reported_as_schedulable(self):
        semester = import_college_data_from_dict(
            PdfImportEndToEndTestCase._payload()
        )["semester"]

        plan = analyse_capacity(semester)

        self.assertEqual(plan["verdict"], "schedulable")
        self.assertEqual(plan["blocking_findings"], [])
        self.assertEqual(plan["division_count"], 4)


class RegressionsFoundInFullAuditTestCase(TestCase):
    """
    Defects found by walking every page and form in the application by hand.

    Each test names the symptom that was observed so a future change cannot
    quietly reintroduce it.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            "audit_admin", "audit@test.com", "password123"
        )
        self.client.force_login(self.admin)
        self.semester = import_college_data_from_dict(
            PdfImportEndToEndTestCase._payload()
        )["semester"]

    # ---------------------------------------------------------------- portal
    def test_admin_without_a_profile_is_sent_home_from_my_timetable(self):
        """A notification links here, so an admin must not hit a 500."""
        response = self.client.get("/my-timetable/")
        self.assertRedirects(response, "/")

    def test_proposal_survives_the_resolve_that_its_own_approval_triggers(self):
        """
        Approving a fix re-runs the solver, which recreates SchedulingIssue rows.
        A CASCADE there deleted the proposal mid-approval and the final save
        failed with "Save with update_fields did not affect any rows".
        """
        proposal = ProposedChange(
            semester=self.semester,
            kind="ADD_ROOM",
            title="Add a room",
            rationale="Extra capacity",
            payload={"name": "Audit Lab", "capacity": 60, "is_lab": True},
        )
        proposal.save()

        # What the solver does at the start of every run.
        SchedulingIssue.objects.filter(semester=self.semester).delete()

        refreshed = ProposedChange.objects.get(pk=proposal.pk)
        self.assertIsNotNone(refreshed)
        refreshed.status = "APPROVED"
        refreshed.save(update_fields=["status"])

    # ----------------------------------------------------------- preferences
    def test_applying_preferences_updates_the_stored_row_instead_of_inserting(self):
        """A second row for the same semester broke the unique constraint."""
        url = f"/preferences/?semester_id={self.semester.id}"
        payload = {
            "working_days": ["MONDAY", "TUESDAY", "WEDNESDAY"],
            "day_start_time": "09:00",
            "day_end_time": "14:00",
            "period_duration_minutes": "60",
            "lunch_start_time": "12:00",
            "lunch_end_time": "13:00",
            "action": "apply",
            "force": "on",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            SchedulingPreference.objects.filter(semester=self.semester).count(), 1
        )

    def test_changing_the_week_shape_retimes_the_existing_periods(self):
        """
        generate_time_slots used get_or_create, so an existing period kept its
        old start/end time and the published grid disagreed with the preference.
        """
        generate_time_slots(
            ["MONDAY"], time(9, 0), time(17, 0), 60,
            (time(13, 0), time(14, 0)),
        )
        slot = TimeSlot.objects.get(day="MON", period_number=1)
        self.assertEqual((slot.start_time, slot.end_time), (time(9, 0), time(10, 0)))

        generate_time_slots(
            ["MONDAY"], time(11, 0), time(17, 0), 45,
            (time(13, 0), time(14, 0)),
        )
        slot.refresh_from_db()
        self.assertEqual((slot.start_time, slot.end_time), (time(11, 0), time(11, 45)))

    def test_blank_time_field_is_a_form_error_not_a_server_error(self):
        """Clearing a time field used to raise ValueError and return HTTP 500."""
        response = self.client.post(
            f"/preferences/?semester_id={self.semester.id}",
            {
                "working_days": ["MONDAY"],
                "day_start_time": "",
                "day_end_time": "",
                "period_duration_minutes": "60",
                "lunch_start_time": "",
                "lunch_end_time": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a time such as")

    def test_blank_period_length_is_a_form_error_not_a_server_error(self):
        response = self.client.post(
            f"/preferences/?semester_id={self.semester.id}",
            {
                "working_days": ["MONDAY"],
                "day_start_time": "09:00",
                "day_end_time": "14:00",
                "period_duration_minutes": "",
                "lunch_start_time": "12:00",
                "lunch_end_time": "13:00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "whole number of minutes")

    # ------------------------------------------------------------ suggestions
    def _force_three_issues_that_share_one_remedy(self):
        """
        Three lab divisions that are all too big for the only lab produce the
        same "add a room" suggestion, which used to be proposed three times.
        """
        room = Room.objects.filter(is_lab=True).order_by("pk").first()
        room.capacity = 5
        room.save(update_fields=["capacity"])

        SchedulingIssue.objects.filter(semester=self.semester).delete()
        lab_assignments = list(
            Assignment.objects.filter(
                semester=self.semester, subject__is_lab=True
            )
        )[:3]
        self.assertGreaterEqual(len(lab_assignments), 2)
        for assignment in lab_assignments:
            assignment.division.strength = 300
            assignment.division.save(update_fields=["strength"])
            SchedulingIssue.objects.create(
                semester=self.semester,
                assignment=assignment,
                hours_requested=2,
                hours_scheduled=0,
                reason=f"{assignment.subject.name} cannot be seated.",
                suggestion="Add a larger laboratory.",
            )
        return lab_assignments

    def test_identical_remedies_are_proposed_only_once(self):
        """One undersized room produced one proposal per affected issue."""
        self._force_three_issues_that_share_one_remedy()

        proposals = recommend_for_semester(self.semester)
        self.assertTrue(proposals)

        signatures = [(p.kind, p.title) for p in proposals]
        self.assertEqual(len(set(signatures)), len(signatures))

    def test_one_approval_clears_every_issue_that_shared_the_remedy(self):
        self._force_three_issues_that_share_one_remedy()

        proposals = recommend_for_semester(self.semester)
        self.assertEqual(len(proposals), 1)

    def test_recommending_twice_does_not_duplicate_pending_proposals(self):
        self._force_three_issues_that_share_one_remedy()

        recommend_for_semester(self.semester)
        before = ProposedChange.objects.filter(
            semester=self.semester, status="PENDING"
        ).count()
        recommend_for_semester(self.semester)
        after = ProposedChange.objects.filter(
            semester=self.semester, status="PENDING"
        ).count()
        self.assertEqual(before, after)
