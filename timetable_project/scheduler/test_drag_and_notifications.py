from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from scheduler.edits import move_entry, placement_conflicts, swap_entries
from scheduler.models import (
    Assignment,
    Notification,
    Room,
    Semester,
    Student,
    Subject,
    Teacher,
    TeacherUnavailability,
    TimeSlot,
    TimetableChangeLog,
    TimetableEntry,
    YearDivision,
)

User = get_user_model()


class DragAndDropAndNotificationsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser("admin_test_user", "admin@vit.edu", "pass1234")
        self.client.force_login(self.admin_user)

        self.teacher1_user = User.objects.create_user("ashwini_deshpande", "ashwini.deshpande@vit.edu", "Teacher@123")
        self.teacher2_user = User.objects.create_user("rahul_sharma", "rahul.sharma@vit.edu", "Teacher@123")

        self.student1_user = User.objects.create_user("student_div_a", "student1@vit.edu", "Student@123")
        self.student2_user = User.objects.create_user("student_div_b", "student2@vit.edu", "Student@123")

        self.semester = Semester.objects.create(
            name="Fall 2026",
            start_date="2026-08-01",
            end_date="2026-12-01",
        )
        self.div_a = YearDivision.objects.create(year=2, division_number=1, strength=60)
        self.div_b = YearDivision.objects.create(year=3, division_number=2, strength=60)

        self.student1 = Student.objects.create(
            user=self.student1_user,
            roll_number="CS2026_01",
            full_name="Aarav Patel",
            division=self.div_a,
            is_active=True,
        )
        self.student2 = Student.objects.create(
            user=self.student2_user,
            roll_number="CS2026_02",
            full_name="Neha Joshi",
            division=self.div_b,
            is_active=True,
        )

        self.teacher1 = Teacher.objects.create(name="Dr. Ashwini Deshpande", max_hours_per_week=20, user=self.teacher1_user)
        self.teacher2 = Teacher.objects.create(name="Prof. Rahul Sharma", max_hours_per_week=20, user=self.teacher2_user)

        self.theory_sub = Subject.objects.create(name="Data Structures", code="CS201", is_lab=False)
        self.lab_sub = Subject.objects.create(name="Database Lab", code="CS202L", is_lab=True)

        self.room_class = Room.objects.create(name="Room 101", is_lab=False, capacity=70)
        self.room_lab = Room.objects.create(name="Lab 201", is_lab=True, capacity=70)

        self.slot1 = TimeSlot.objects.create(day="MON", period_number=1, start_time="09:00", end_time="10:00")
        self.slot2 = TimeSlot.objects.create(day="MON", period_number=2, start_time="10:00", end_time="11:00")
        self.slot3 = TimeSlot.objects.create(day="TUE", period_number=1, start_time="09:00", end_time="10:00")

        self.assign1 = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher1,
            subject=self.theory_sub,
            division=self.div_a,
            total_hours_for_semester=60,
        )
        self.assign2 = Assignment.objects.create(
            semester=self.semester,
            teacher=self.teacher2,
            subject=self.lab_sub,
            division=self.div_b,
            total_hours_for_semester=60,
        )

        self.entry1 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign1,
            room=self.room_class,
            time_slot=self.slot1,
        )

    def test_placement_conflicts_detailed_explanations(self):
        # 1. Room Occupancy conflict gives full forensic details
        conflicts = placement_conflicts(self.assign2, self.slot1, self.room_class)
        self.assertTrue(any("already occupied" in c for c in conflicts))
        self.assertTrue(any(self.theory_sub.name in c for c in conflicts))
        self.assertTrue(any(self.teacher1.name in c for c in conflicts))
        self.assertTrue(any(self.div_a.name in c for c in conflicts))

        # 2. Lab Mismatch conflict
        conflicts_lab = placement_conflicts(self.assign2, self.slot2, self.room_class)
        self.assertTrue(any("Mismatch" in c for c in conflicts_lab))

        # 3. Teacher unavailability
        TeacherUnavailability.objects.create(teacher=self.teacher1, time_slot=self.slot2)
        conflicts_unavail = placement_conflicts(self.assign1, self.slot2, self.room_class, [self.entry1.id])
        self.assertTrue(any("unavailable" in c.lower() for c in conflicts_unavail))

    def test_live_move_api_success_and_instant_portal_notifications(self):
        url = reverse("scheduler:live_move_api")
        res = self.client.post(url, {
            "entry_id": self.entry1.id,
            "day": "TUE",
            "period": "1",
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["teacher_name"], self.teacher1.name)

        self.entry1.refresh_from_db()
        self.assertEqual(self.entry1.time_slot.slot_code, "TUE-P1")

        # 1. Verify instant in-app portal notification was sent to teacher1
        notif_teacher = Notification.objects.filter(recipient=self.teacher1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_teacher, "Notification must be created for teacher on their portal")
        self.assertEqual(notif_teacher.kind, "TIMETABLE_CHANGED")
        self.assertIn("Data Structures", notif_teacher.title)
        self.assertIn("TUE-P1", notif_teacher.title)
        self.assertIn("MON-P1", notif_teacher.body)
        self.assertFalse(notif_teacher.is_read)

        # 2. Verify instant in-app portal notification was sent to student in div_a
        notif_student = Notification.objects.filter(recipient=self.student1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_student, "Notification must be created for student on their portal")
        self.assertEqual(notif_student.kind, "TIMETABLE_CHANGED")
        self.assertIn("Data Structures", notif_student.body)
        self.assertIn(self.teacher1.name, notif_student.body)

    def test_live_swap_api_success_and_instant_portal_notifications(self):
        entry2 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign2,
            room=self.room_lab,
            time_slot=self.slot2,
        )
        url = reverse("scheduler:live_swap_api")
        res = self.client.post(url, {
            "entry_a_id": self.entry1.id,
            "entry_b_id": entry2.id,
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])

        # Check teacher 1 notification
        notif1 = Notification.objects.filter(recipient=self.teacher1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif1)
        self.assertIn("swapped", notif1.body.lower())
        self.assertIn("Data Structures", notif1.body)

        # Check teacher 2 notification
        notif2 = Notification.objects.filter(recipient=self.teacher2_user).order_by("-created_at").first()
        self.assertIsNotNone(notif2)
        self.assertIn("swapped", notif2.body.lower())
        self.assertIn("Database Lab", notif2.body)

        # Check student 1 notification
        notif_s1 = Notification.objects.filter(recipient=self.student1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_s1)
        self.assertIn("swapped", notif_s1.body.lower())

        # Check student 2 notification
        notif_s2 = Notification.objects.filter(recipient=self.student2_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_s2)
        self.assertIn("swapped", notif_s2.body.lower())

    def test_live_move_api_rejects_with_detailed_conflicts(self):
        # Create second entry at slot2
        entry2 = TimetableEntry.objects.create(
            semester=self.semester,
            assignment=self.assign2,
            room=self.room_lab,
            time_slot=self.slot2,
        )
        url = reverse("scheduler:live_move_api")
        # Try moving entry1 into slot2 in room_lab (wrong room type + division/room conflict)
        res = self.client.post(url, {
            "entry_id": self.entry1.id,
            "target_slot_id": self.slot2.id,
            "target_room_id": self.room_lab.id,
        })
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data["success"])
        self.assertIn("conflicts", data)
        self.assertTrue(len(data["conflicts"]) > 0)

    def test_delete_entry_delivers_portal_notification(self):
        url = reverse("scheduler:delete_entry", args=[self.entry1.id])
        res = self.client.post(url)
        self.assertEqual(res.status_code, 302)

        # Teacher received class cancellation notification
        notif_t = Notification.objects.filter(recipient=self.teacher1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_t)
        self.assertIn("Cancelled", notif_t.title)

        # Student received class cancellation notification
        notif_s = Notification.objects.filter(recipient=self.student1_user).order_by("-created_at").first()
        self.assertIsNotNone(notif_s)
        self.assertIn("Cancelled", notif_s.title)
