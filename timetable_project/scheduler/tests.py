from collections import Counter

from django.contrib.auth import get_user_model
from django.test import TestCase

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
    TimeSlot,
    generate_time_slots,
)
from scheduler.edits import delete_entry, move_entry, placement_conflicts, swap_entries
from scheduler.importer import import_college_data_from_dict
from scheduler.solver import generate_timetable, TimetableSolverError


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
        plain = User.objects.create_user("limited_viewer", password="password123")

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
        self.assertContains(response, "Upload JSON")
        self.assertContains(response, "Manage")

        self.client.logout()
        self.client.login(username="stu_div1", password="password123")
        response = self.client.get("/my-timetable/")
        self.assertContains(response, "My Timetable")
        self.assertNotContains(response, "Upload JSON")

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

