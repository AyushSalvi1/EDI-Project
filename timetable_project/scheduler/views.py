import json

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    Student,
    TimetableEntry,
    TimetableChangeLog,
    SchedulingIssue,
    DAY_CHOICES,
    DAY_ORDER,
)
from .edits import delete_entry, move_entry, swap_entries, available_rooms_for
from .importer import import_college_data_from_json
from .roles import (
    STUDENT_GROUP,
    admin_required,
    is_admin,
    landing_page_for,
    student_profile,
    teacher_profile,
)
from .solver import generate_timetable, TimetableSolverError

MAX_UPLOAD_SIZE = 5 * 1024 * 1024
ENTRIES_PER_PAGE = 40


# ===========================================================================
# Shared helpers
# ===========================================================================
def _safe_redirect_target(request, raw):
    """Only follow a relative 'next' target that points back at this host."""
    if raw and url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return raw
    return None


def _build_grid(entries, time_slots):
    """
    Turn a flat list of TimetableEntry rows into the row/cell structure the
    Day x Period grid template needs.
    """
    days_present = [key for key, _ in DAY_CHOICES if any(s.day == key for s in time_slots)]

    period_numbers = sorted({s.period_number for s in time_slots})
    period_info = {}
    for slot in time_slots:
        period_info.setdefault(
            slot.period_number, f"{slot.start_time.strftime('%H:%M')} - {slot.end_time.strftime('%H:%M')}"
        )

    grid = {p: {d: None for d in days_present} for p in period_numbers}
    for entry in entries:
        row = grid.get(entry.time_slot.period_number)
        if row is not None and entry.time_slot.day in row:
            row[entry.time_slot.day] = entry

    grid_rows = []
    for period in period_numbers:
        grid_rows.append(
            {
                "period_number": period,
                "time_range": period_info.get(period, ""),
                "cells": [
                    {"day": day, "entry": grid[period].get(day)} for day in days_present
                ],
            }
        )
    return [dict(DAY_CHOICES)[d] for d in days_present], grid_rows


# ===========================================================================
# Authentication
# ===========================================================================
def login_view(request):
    """Role-aware sign-in page for admins, teachers and students."""
    if request.user.is_authenticated:
        return redirect(landing_page_for(request.user))

    form = AuthenticationForm(request, data=request.POST or None)
    next_url = _safe_redirect_target(request, request.POST.get("next") or request.GET.get("next"))

    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect(next_url or landing_page_for(request.user))

    if request.method == "POST":
        messages.error(request, "Invalid username or password.")

    return render(
        request,
        "scheduler/login.html",
        {
            "form": form,
            "next": next_url or "",
            "divisions": YearDivision.objects.all().order_by("year", "division_number"),
        },
    )


@login_required
def logout_view(request):
    """Log out the current user and return to the sign-in page."""
    if request.method == "POST":
        logout(request)
        messages.success(request, "You have been signed out.")
        return redirect("scheduler:login")
    return render(request, "scheduler/logout_confirm.html")


def register_student_view(request):
    """
    Student self-registration.

    Only students may self-register. Teacher and admin accounts are provisioned
    by an administrator so that a login can never be linked to the wrong
    academic record.
    """
    if request.user.is_authenticated:
        return redirect(landing_page_for(request.user))

    divisions = YearDivision.objects.all().order_by("year", "division_number")
    form_data = {
        "full_name": request.POST.get("full_name", "").strip(),
        "roll_number": request.POST.get("roll_number", "").strip(),
        "email": request.POST.get("email", "").strip(),
        "division_id": request.POST.get("division_id", ""),
        "username": request.POST.get("username", "").strip(),
    }
    errors = []

    if request.method == "POST":
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")

        if not all([form_data["full_name"], form_data["roll_number"], form_data["username"]]):
            errors.append("Full name, roll number and username are all required.")
        if not form_data["division_id"]:
            errors.append("Please select your year and division.")
        if password1 != password2:
            errors.append("The two passwords do not match.")
        if len(password1) < 8:
            errors.append("Password must be at least 8 characters long.")

        if User.objects.filter(username__iexact=form_data["username"]).exists():
            errors.append("That username is already taken.")

        if Student.objects.filter(roll_number__iexact=form_data["roll_number"]).exists():
            errors.append("That roll number is already registered.")

        division = None
        if not errors:
            division = YearDivision.objects.filter(pk=form_data["division_id"]).first()
            if division is None:
                errors.append("The selected division does not exist.")

        if not errors:
            user = User.objects.create_user(
                username=form_data["username"],
                password=password1,
                email=form_data["email"],
                first_name=form_data["full_name"],
            )
            Student.objects.create(
                user=user,
                roll_number=form_data["roll_number"],
                full_name=form_data["full_name"],
                email=form_data["email"],
                division=division,
            )
            group, _ = Group.objects.get_or_create(name=STUDENT_GROUP)
            user.groups.add(group)

            login(request, user)
            messages.success(request, f"Welcome, {form_data['full_name']}! Your account is ready.")
            return redirect("scheduler:my_timetable")

    return render(
        request,
        "scheduler/register.html",
        {
            "errors": errors,
            "form_data": form_data,
            "divisions": divisions,
        },
    )


# ===========================================================================
# Dashboards
# ===========================================================================
@login_required
def home_view(request):
    """
    Administrators get the full metrics dashboard; teachers and students are
    routed straight to their own read-only timetable.
    """
    if not is_admin(request.user):
        return redirect("scheduler:my_timetable")

    semesters = Semester.objects.all().order_by("-start_date")
    active_semester = semesters.first()

    context = {
        "semesters": semesters,
        "active_semester": active_semester,
        "total_divisions": YearDivision.objects.count(),
        "total_rooms": Room.objects.count(),
        "total_teachers": Teacher.objects.count(),
        "total_subjects": Subject.objects.count(),
        "total_students": Student.objects.count(),
        "total_entries": TimetableEntry.objects.filter(semester=active_semester).count() if active_semester else 0,
        "total_issues": SchedulingIssue.objects.filter(semester=active_semester).count() if active_semester else 0,
    }
    return render(request, "scheduler/dashboard.html", context)


@login_required
def my_timetable_view(request):
    """
    Read-only personal timetable.

    A teacher sees the classes allocated to them across every division; a
    student sees the schedule of their own division. Neither can select another
    division, teacher or room, and there are no editing controls.
    """
    user = request.user
    teacher = teacher_profile(user)
    student = student_profile(user)
    scope = "teacher" if teacher else "student"

    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id")
    if semester_id:
        semester = get_object_or_404(Semester, pk=semester_id)
    else:
        semester = semesters.first()

    time_slots = list(TimeSlot.objects.all())

    if teacher:
        entries_qs = TimetableEntry.objects.filter(assignment__teacher=teacher)
    else:
        entries_qs = TimetableEntry.objects.filter(assignment__division=student.division)

    if semester:
        entries_qs = entries_qs.filter(semester=semester)
    else:
        entries_qs = entries_qs.none()

    entries = list(
        entries_qs.select_related(
            "assignment__subject", "assignment__teacher", "assignment__division", "room", "time_slot"
        )
    )
    day_headers, grid_rows = _build_grid(entries, time_slots)

    weekly_load = 0
    hours_by_subject = {}
    if teacher and semester:
        weekly_load = sum(a.weekly_hours() for a in teacher.assignments.filter(semester=semester))
        for entry in entries:
            key = entry.assignment.subject.name
            hours_by_subject[key] = hours_by_subject.get(key, 0) + 1

    context = {
        "scope": scope,
        "semesters": semesters,
        "selected_semester": semester,
        "teacher": teacher,
        "student": student,
        "day_headers": day_headers,
        "grid_rows": grid_rows,
        "entries": entries,
        "weekly_load": weekly_load,
        "max_hours": teacher.max_hours_per_week if teacher else None,
        "hours_by_subject": hours_by_subject,
        "show_division": scope == "teacher",
        "show_teacher": scope == "student",
    }
    return render(request, "scheduler/my_timetable.html", context)


# ===========================================================================
# Administrator: data import & solver
# ===========================================================================
@admin_required
def upload_json_view(request):
    """Page to upload college data JSON file with validation error reporting."""
    if request.method == "POST":
        uploaded_file = request.FILES.get("json_file")
        if not uploaded_file:
            messages.error(request, "Please select a JSON file to upload.")
            return render(request, "scheduler/upload_json.html")

        if not uploaded_file.name.lower().endswith(".json"):
            messages.error(request, "Invalid file extension. Please upload a .json file.")
            return render(request, "scheduler/upload_json.html")

        if uploaded_file.size > MAX_UPLOAD_SIZE:
            messages.error(request, "The JSON file is too large. Maximum allowed size is 5 MB.")
            return render(request, "scheduler/upload_json.html")

        if uploaded_file.content_type not in {"application/json", "application/*+json"}:
            messages.error(request, "Invalid file type. Please upload a JSON file with content type application/json.")
            return render(request, "scheduler/upload_json.html")

        try:
            result = import_college_data_from_json(uploaded_file)
            semester = result["semester"]
            messages.success(
                request,
                f"Successfully imported data for '{semester.name}'! "
                f"Created {result['divisions_count']} divisions, {result['subjects_count']} subjects, "
                f"{result['rooms_count']} rooms, {result['teachers_count']} teachers, and "
                f"{result['assignments_count']} assignments."
            )
            return redirect(reverse("scheduler:generate_timetable") + f"?semester_id={semester.id}")
        except ValidationError as ve:
            error_details = ve.message if hasattr(ve, "message") else str(ve)
            messages.error(request, f"JSON Validation Error:\n{error_details}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            messages.error(request, "Import failed because the file is not valid UTF-8 JSON.")
        except Exception:
            messages.error(request, "Import failed due to an unexpected server error. Please check the file and try again.")

    return render(request, "scheduler/upload_json.html")


@admin_required
def generate_timetable_view(request):
    """Page to trigger timetable generation and display results/issues."""
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id") or request.POST.get("semester_id")

    if semester_id:
        semester = get_object_or_404(Semester, pk=semester_id)
    else:
        semester = semesters.first()

    generation_result = None

    if request.method == "POST" and semester:
        try:
            generation_result = generate_timetable(semester.id)
            messages.success(request, generation_result["message"])
            TimetableChangeLog.objects.create(
                semester=semester,
                changed_by=request.user,
                action="REGEN",
                detail=(
                    f"CP-SAT re-solve: {generation_result['total_hours_scheduled']}/"
                    f"{generation_result['total_hours_requested']} hours scheduled, "
                    f"{generation_result['issues_count']} issues."
                ),
            )
        except TimetableSolverError as tse:
            messages.error(request, f"Solver Error: {str(tse)}")
        except Exception as e:
            messages.error(request, f"Unexpected Error during generation: {str(e)}")

    issues = []
    entries_count = 0
    if semester:
        issues = SchedulingIssue.objects.filter(semester=semester).select_related(
            "assignment__division", "assignment__subject", "assignment__teacher"
        )
        entries_count = TimetableEntry.objects.filter(semester=semester).count()

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "generation_result": generation_result,
        "issues": issues,
        "entries_count": entries_count,
    }
    return render(request, "scheduler/generate.html", context)


@admin_required
def timetable_view(request):
    """Search and view timetable for any single division in a Day x Period grid."""
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id")
    division_id = request.GET.get("division_id")

    selected_semester = None
    if semester_id:
        selected_semester = get_object_or_404(Semester, pk=semester_id)
    elif semesters.exists():
        selected_semester = semesters.first()

    divisions = YearDivision.objects.all().order_by("year", "division_number")
    selected_division = None
    if division_id:
        selected_division = get_object_or_404(YearDivision, pk=division_id)
    elif divisions.exists():
        selected_division = divisions.first()

    time_slots = list(TimeSlot.objects.all())

    entries_qs = TimetableEntry.objects.select_related(
        "assignment__subject", "assignment__teacher", "assignment__division", "room", "time_slot"
    )
    if selected_semester:
        entries_qs = entries_qs.filter(semester=selected_semester)
    if selected_division:
        entries_qs = entries_qs.filter(assignment__division=selected_division)
    entries = list(entries_qs)

    day_headers, grid_rows = _build_grid(entries, time_slots)

    context = {
        "semesters": semesters,
        "selected_semester": selected_semester,
        "divisions": divisions,
        "selected_division": selected_division,
        "day_headers": day_headers,
        "grid_rows": grid_rows,
        "show_division": True,
        "show_teacher": True,
    }
    return render(request, "scheduler/timetable_view.html", context)


# ===========================================================================
# Administrator: manual timetable changes
# ===========================================================================
@admin_required
def manage_entries_view(request):
    """
    Filterable list of every scheduled entry, with delete and change history.
    This is the administrator's control panel for the published timetable.
    """
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id")
    if semester_id:
        semester = get_object_or_404(Semester, pk=semester_id)
    else:
        semester = semesters.first()

    entries_qs = TimetableEntry.objects.select_related(
        "assignment__subject", "assignment__teacher", "assignment__division", "room", "time_slot"
    )
    if semester:
        entries_qs = entries_qs.filter(semester=semester)

    day = request.GET.get("day") or ""
    room_id = request.GET.get("room_id") or ""
    division_id = request.GET.get("division_id") or ""
    teacher_id = request.GET.get("teacher_id") or ""
    query = (request.GET.get("q") or "").strip()

    if day:
        entries_qs = entries_qs.filter(time_slot__day=day)
    if room_id:
        entries_qs = entries_qs.filter(room_id=room_id)
    if division_id:
        entries_qs = entries_qs.filter(assignment__division_id=division_id)
    if teacher_id:
        entries_qs = entries_qs.filter(assignment__teacher_id=teacher_id)
    if query:
        entries_qs = entries_qs.filter(
            Q(assignment__subject__name__icontains=query)
            | Q(assignment__teacher__name__icontains=query)
            | Q(assignment__division__name__icontains=query)
            | Q(room__name__icontains=query)
        )

    entries_qs = entries_qs.order_by("time_slot__day", "time_slot__period_number", "room__name")

    paginator = Paginator(entries_qs, ENTRIES_PER_PAGE)
    page = paginator.get_page(request.GET.get("page"))

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "rooms": Room.objects.all().order_by("is_lab", "name"),
        "divisions": YearDivision.objects.all().order_by("year", "division_number"),
        "teachers": Teacher.objects.all().order_by("name"),
        "day_choices": DAY_CHOICES,
        "days_with_slots": sorted(
            {s.day for s in TimeSlot.objects.all()}, key=lambda d: DAY_ORDER.get(d, 99)
        ),
        "selected_day": day,
        "selected_room_id": room_id,
        "selected_division_id": division_id,
        "selected_teacher_id": teacher_id,
        "query": query,
        "page": page,
        "recent_changes": (
            TimetableChangeLog.objects.filter(semester=semester).select_related("changed_by")[:10]
            if semester
            else TimetableChangeLog.objects.none()
        ),
    }
    return render(request, "scheduler/manage_entries.html", context)


@admin_required
def delete_entry_view(request, pk):
    """Remove one scheduled class from the timetable."""
    entry = get_object_or_404(
        TimetableEntry.objects.select_related(
            "assignment__subject", "assignment__teacher", "assignment__division", "room", "time_slot"
        ),
        pk=pk,
    )
    semester_id = entry.semester_id

    if request.method == "POST":
        subject = entry.assignment.subject.name
        division = entry.assignment.division.name
        slot = entry.time_slot.slot_code
        room = entry.room.name
        delete_entry(entry, user=request.user)
        messages.success(
            request,
            f"Removed '{subject}' for {division} at {slot} in {room}. "
            "Re-run the solver if you want the hours redistributed.",
        )
        return redirect(reverse("scheduler:manage_entries") + f"?semester_id={semester_id}")

    return render(request, "scheduler/delete_entry_confirm.html", {"entry": entry})


@admin_required
def move_entry_view(request, pk):
    """Reschedule a single entry to a different period and/or room."""
    entry = get_object_or_404(
        TimetableEntry.objects.select_related(
            "assignment__subject", "assignment__teacher", "assignment__division", "room", "time_slot"
        ),
        pk=pk,
    )
    slots = TimeSlot.objects.all().order_by("day", "period_number")
    legal_rooms = available_rooms_for(entry.assignment)

    if request.method == "POST":
        new_slot = TimeSlot.objects.filter(pk=request.POST.get("time_slot_id")).first()
        new_room = Room.objects.filter(pk=request.POST.get("room_id")).first()
        if new_slot is None or new_room is None:
            messages.error(request, "Please choose both a period and a room.")
        else:
            _, conflicts = move_entry(entry, new_slot, new_room, user=request.user)
            if conflicts:
                for conflict in conflicts:
                    messages.error(request, conflict)
            else:
                messages.success(
                    request,
                    f"'{entry.assignment.subject.name}' moved to "
                    f"{new_slot.slot_code} in {new_room.name}.",
                )
                return redirect(
                    reverse("scheduler:manage_entries") + f"?semester_id={entry.semester_id}"
                )
        entry.refresh_from_db()

    context = {
        "entry": entry,
        "slots": slots,
        "rooms": legal_rooms,
        "selected_slot_id": request.POST.get("time_slot_id") or entry.time_slot_id,
        "selected_room_id": request.POST.get("room_id") or entry.room_id,
    }
    return render(request, "scheduler/move_entry.html", context)


@admin_required
def swap_entries_view(request):
    """Exchange the positions of two scheduled entries."""
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id") or request.POST.get("semester_id")
    if semester_id:
        semester = get_object_or_404(Semester, pk=semester_id)
    else:
        semester = semesters.first()

    choices = TimetableEntry.objects.none()
    if semester:
        choices = TimetableEntry.objects.filter(semester=semester).select_related(
            "assignment__subject", "assignment__division", "assignment__teacher", "time_slot", "room"
        ).order_by("time_slot__day", "time_slot__period_number")

    if request.method == "POST":
        entry_a = TimetableEntry.objects.filter(
            pk=request.POST.get("entry_a"), semester=semester
        ).select_related("assignment__subject", "assignment__teacher", "assignment__division", "time_slot", "room").first()
        entry_b = TimetableEntry.objects.filter(
            pk=request.POST.get("entry_b"), semester=semester
        ).select_related("assignment__subject", "assignment__teacher", "assignment__division", "time_slot", "room").first()

        if not entry_a or not entry_b:
            messages.error(request, "Please choose two timetable entries to swap.")
        else:
            ok, conflicts = swap_entries(entry_a, entry_b, user=request.user)
            if ok:
                messages.success(
                    request,
                    f"Swapped '{entry_a.assignment.subject.name}' with "
                    f"'{entry_b.assignment.subject.name}'.",
                )
            else:
                for conflict in conflicts:
                    messages.error(request, conflict)
        return redirect(reverse("scheduler:swap_entries") + f"?semester_id={semester.id if semester else ''}")

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "choices": choices,
    }
    return render(request, "scheduler/swap_entries.html", context)