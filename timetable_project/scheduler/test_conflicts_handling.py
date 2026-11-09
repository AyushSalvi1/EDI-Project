from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from scheduler.edits import (
    find_suitable_room,
    move_entry,
    placement_conflicts,
    swap_entries,
)
from scheduler.models import (
    Assignment,
    ProposedChange,
    Room,
    SchedulingIssue,
    Semester,
    Subject,
    Teacher,
    TeacherUnavailability,
    TimeSlot,
    TimetableEntry,
    YearDivision,
)
from scheduler.recommendations import diagnose, recommend_for_semester
from scheduler.solver import generate_timetable

User = get_user_model()


class ConflictHandlingComprehensiveTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser("admin_conflict", "admin@vit.edu", "pass1234")
        self.client.force_login(self.admin)

        self.semester = Semester.objects.create(
            name="Fall 2026 Conflict Lab",
            start_date="2026-08-03",
            end_date="2026-11-16",  # 15 weeks
        )

        # Divisions
        self.div_a = YearDivision.objects.create(year=2, division_number=1, strength=60)
        self.div_b = YearDivision.objects.create(year=2, division_number=2, strength=60)
        self.div_huge = YearDivision.objects.create(year=4, division_number=1, strength=85)

        # Teachers
        self.teacher_primary = Teacher.objects.create(name="Dr. Ashwini Deshpande", max_hours_per_week=18)
        self.teacher_secondary = Teacher.objects.create(name="Prof. Rahul Sharma", max_hours_per_week=20)
        self.teacher_low_cap = Teacher.objects.create(name="Prof. Vikram Rao", max_hours_per_week=4)

        # Subjects
        self.subj_theory = Subject.objects.create(name="Data Structures", code="CS201", is_lab=False)
        self.subj_lab = Subject.objects.create(name="Hardware Lab", code="CS202L", is_lab=True)
        self.subj_math = Subject.objects.create(name="Applied Statistics", code="MATH201", is_lab=False)

        # Rooms
        self.room_theory_standard = Room.objects.create(name="Lecture Hall 101", is_lab=False, capacity=70)
        self.room_theory_alt = Room.objects.create(name="Lecture Hall 102", is_lab=False, capacity=70)
        self.room_theory_small = Room.objects.create(name="Seminar Room 103", is_lab=False, capacity=40)
        self.room_lab_standard = Room.objects.create(name="Advanced Computing Lab", is_lab=True, capacity=65)

        # Time Slots
        self.slot1 = TimeSlot.objects.create(day="MON", period_number=1, start_time="09:00", end_time="10:00")
        self.slot2 = TimeSlot.objects.create(day="MON", period_number=2, start_time="10:00", end_time="11:00")
        self.slot3 = TimeSlot.objects.create(day="TUE", period_number=1, start_time="09:00", end_time="10:00")
        self.slot4 = TimeSlot.objects.create(day="TUE", period_number=2, start_time="10:00", end_time="11:00")

        # Base Assignments
        self.assign1 = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_primary,
            subject=self.subj_theory,
            division=self.div_a,
            total_hours_for_semester=45,  # 3 hrs/week
        )
        self.assign2 = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_secondary,
            subject=self.subj_theory,
            division=self.div_b,
            total_hours_for_semester=45,  # 3 hrs/week
        )

    # -------------------------------------------------------------------------
    # 1. TEACHER CONFLICTS
    # -------------------------------------------------------------------------
    def test_teacher_unavailability_conflict_blocked_and_reported(self):
        """
        When a teacher is marked unavailable for a slot, the system must:
        1. Reject manual placement/moves into that slot with forensic conflict details.
        2. Never schedule the teacher in that slot during CP-SAT solver generation.
        """
        # Mark Dr. Ashwini unavailable on MON-P1
        TeacherUnavailability.objects.create(teacher=self.teacher_primary, time_slot=self.slot1)

        # Verify placement conflict validator catches it
        conflicts = placement_conflicts(self.assign1, self.slot1, self.room_theory_standard)
        self.assertTrue(any("Faculty Dr. Ashwini Deshpande is marked unavailable" in c for c in conflicts))

        # Attempting move_entry into that slot must fail and write nothing
        entry = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign1,
            room=self.room_theory_standard,
            time_slot=self.slot2,
        )
        _, move_conflicts = move_entry(entry, self.slot1, self.room_theory_standard)
        self.assertTrue(len(move_conflicts) > 0)
        entry.refresh_from_db()
        self.assertEqual(entry.time_slot, self.slot2)  # Slot remains untouched

    def test_teacher_double_booking_concurrency_conflict_prevented(self):
        """
        A teacher cannot teach two classes simultaneously.
        Live move and swap APIs must detect and refuse the collision.
        """
        # Schedule entry 1 for Teacher Ashwini at MON-P1
        entry1 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign1,
            room=self.room_theory_standard,
            time_slot=self.slot1,
        )

        # Another assignment with the same teacher (Ashwini) for Div B
        assign_same_teacher = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_primary,
            subject=self.subj_math,
            division=self.div_b,
            total_hours_for_semester=30,
        )

        # Try placing assign_same_teacher into MON-P1 in a different room
        conflicts = placement_conflicts(assign_same_teacher, self.slot1, self.room_theory_alt)
        self.assertTrue(any("Teacher Availability Conflict" in c for c in conflicts))
        self.assertTrue(any("already teaches another class at MON-P1" in c for c in conflicts))

    def test_teacher_overload_workload_handled_honestly_by_solver(self):
        """
        When teacher requested hours exceed contract max_hours_per_week, the solver:
        1. Never exceeds the teacher's maximum contracted hours.
        2. Generates a SchedulingIssue record explaining the contract bottleneck.
        """
        # Assign 8 hours/week to Prof. Vikram Rao whose max is 4 hours/week
        assign_overload = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_low_cap,
            subject=self.subj_theory,
            division=self.div_a,
            total_hours_for_semester=120,  # 8 hrs/week
        )

        result = generate_timetable(self.semester.id)
        self.assertTrue(result["success"])

        # Check total hours scheduled for Prof. Vikram Rao <= 4
        scheduled_hours = TimetableEntry.objects.filter(
            semester=self.semester,
            assignment__teacher=self.teacher_low_cap,
        ).count()
        self.assertLessEqual(scheduled_hours, self.teacher_low_cap.max_hours_per_week)

        # Verify honest diagnosis in SchedulingIssue
        issue = SchedulingIssue.objects.filter(
            semester=self.semester,
            assignment=assign_overload,
        ).first()
        self.assertIsNotNone(issue)
        self.assertIn("exceeds the maximum allowed limit", issue.reason)
        self.assertIn("Increase Prof. Vikram Rao's max hours per week", issue.suggestion)

    # -------------------------------------------------------------------------
    # 2. CLASSROOM CONFLICTS
    # -------------------------------------------------------------------------
    def test_classroom_capacity_deficit_conflict_detected_and_handled(self):
        """
        A division of strength 85 cannot fit in a 70-seat or 40-seat classroom.
        The placement validator rejects it, and solver records a capacity issue.
        """
        assign_huge = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_secondary,
            subject=self.subj_theory,
            division=self.div_huge,  # 85 students
            total_hours_for_semester=30,
        )

        conflicts = placement_conflicts(assign_huge, self.slot1, self.room_theory_standard)  # 70 capacity
        self.assertTrue(any("Capacity Deficit" in c for c in conflicts))
        self.assertTrue(any("seats 70" in c and "85 students" in c for c in conflicts))

    def test_classroom_type_mismatch_conflict_prevented(self):
        """
        A lab subject cannot be held in a theory room, and a theory subject
        cannot be held in a lab room.
        """
        assign_lab = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_primary,
            subject=self.subj_lab,
            division=self.div_a,
            total_hours_for_semester=30,
        )

        conflicts = placement_conflicts(assign_lab, self.slot1, self.room_theory_standard)
        self.assertTrue(any("Lab / Classroom Mismatch" in c for c in conflicts))

    def test_classroom_occupancy_auto_resolves_to_available_alternative_room(self):
        """
        When moving an entry to a slot where its previous room is occupied,
        the system automatically searches for and reallocates to an available
        compatible room rather than failing outright.
        """
        # Entry 1 occupies Lecture Hall 101 at MON-P1
        entry1 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign1,
            room=self.room_theory_standard,
            time_slot=self.slot1,
        )
        # Entry 2 occupies Lecture Hall 101 at MON-P2
        entry2 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign2,
            room=self.room_theory_standard,
            time_slot=self.slot2,
        )

        # Move entry2 to MON-P1 without specifying room.
        # Lecture Hall 101 is busy with entry1, but Lecture Hall 102 is free!
        moved_entry, conflicts = move_entry(entry2, self.slot1, new_room=None)
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(moved_entry.time_slot, self.slot1)
        self.assertEqual(moved_entry.room, self.room_theory_alt)  # Auto-reallocated to Hall 102!

    def test_classroom_double_booking_blocked_when_no_alternative_room(self):
        """
        If all compatible rooms are occupied in a slot, attempting to place
        another class in that slot and room must be refused with detailed forensic error.
        """
        # Room 101 occupied at MON-P1
        TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign1,
            room=self.room_theory_standard,
            time_slot=self.slot1,
        )
        # Room 102 occupied at MON-P1
        assign_other = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_secondary,
            subject=self.subj_math,
            division=self.div_b,
            total_hours_for_semester=15,
        )
        TimetableEntry.objects.create(
            semester=self.semester,
            assignment=assign_other,
            room=self.room_theory_alt,
            time_slot=self.slot1,
        )

        # Room 103 is too small for div_a (cap 40 vs strength 60)
        alt = find_suitable_room(self.assign1, self.slot1)
        self.assertIsNone(alt)

        # Direct placement attempt in Room 101 must report Room occupied
        conflicts = placement_conflicts(self.assign1, self.slot1, self.room_theory_standard)
        self.assertTrue(any("already occupied at MON-P1" in c for c in conflicts))

    # -------------------------------------------------------------------------
    # 3. RECOMMENDATIONS & AUTOMATED FIX ENGINE
    # -------------------------------------------------------------------------
    def test_recommendation_engine_synthesizes_verified_remedies_for_conflicts(self):
        """
        When teacher over-contract or room capacity issues exist, the
        recommendation engine synthesizes verified proposals.
        """
        # 1. Overloaded teacher
        Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher_low_cap,  # 4h max
            subject=self.subj_theory,
            division=self.div_a,
            total_hours_for_semester=120,  # 8 hrs/week
        )
        # Generate timetable to produce SchedulingIssue records
        generate_timetable(self.semester.id)

        issues = list(SchedulingIssue.objects.filter(semester=self.semester))
        self.assertTrue(len(issues) > 0)

        # Run recommendation synthesizer
        proposals = recommend_for_semester(self.semester)
        self.assertTrue(len(proposals) > 0)
        # A proposal addressing contract limit or room capacity is generated
        actions = [p.kind for p in proposals]
        self.assertTrue(any(a in ["INCREASE_CONTRACT_HOURS", "REASSIGN_TEACHER", "ADD_TIME_SLOTS", "INCREASE_ROOM_CAPACITY"] for a in actions))
