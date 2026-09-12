from collections import Counter
from django.test import TestCase
from django.core.exceptions import ValidationError

from scheduler.models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    TeacherUnavailability,
    Assignment,
    TimetableEntry,
    SchedulingIssue,
    generate_time_slots,
)
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
        import json
        from django.contrib.auth import get_user_model
        User = get_user_model()
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

    def test_upload_json_view_get(self):
        response = self.client.get("/upload/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload College Schedule Data")

    def test_upload_json_view_post_valid(self):
        import io, json
        from django.core.files.uploadedfile import SimpleUploadedFile

        json_bytes = json.dumps(self.sample_data).encode("utf-8")
        uploaded = SimpleUploadedFile("test_college.json", json_bytes, content_type="application/json")

        response = self.client.post("/upload/", {"json_file": uploaded}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Successfully imported data")

    def test_generate_timetable_view(self):
        response = self.client.post("/generate/", {"semester_id": self.semester.id}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Successfully generated timetable")

    def test_timetable_view_grid(self):
        # Generate timetable first
        generate_timetable(self.semester.id)
        div = YearDivision.objects.first()
        response = self.client.get(f"/timetable/?semester_id={self.semester.id}&division_id={div.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, div.name)
        self.assertContains(response, "Period 1")

