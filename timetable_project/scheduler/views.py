from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from .models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    Assignment,
    TimetableEntry,
    SchedulingIssue,
    DAY_CHOICES,
)
from .importer import import_college_data_from_json
from .solver import generate_timetable, TimetableSolverError


def home_view(request):
    """Dashboard view showing overview stats and quick navigation."""
    semesters = Semester.objects.all().order_by("-start_date")
    active_semester = semesters.first()

    context = {
        "semesters": semesters,
        "active_semester": active_semester,
        "total_divisions": YearDivision.objects.count(),
        "total_rooms": Room.objects.count(),
        "total_teachers": Teacher.objects.count(),
        "total_subjects": Subject.objects.count(),
        "total_entries": TimetableEntry.objects.filter(semester=active_semester).count() if active_semester else 0,
        "total_issues": SchedulingIssue.objects.filter(semester=active_semester).count() if active_semester else 0,
    }
    return render(request, "scheduler/dashboard.html", context)


def upload_json_view(request):
    """Page to upload college data JSON file with validation error reporting."""
    if request.method == "POST":
        uploaded_file = request.FILES.get("json_file")
        if not uploaded_file:
            messages.error(request, "Please select a JSON file to upload.")
            return render(request, "scheduler/upload_json.html")

        if not uploaded_file.name.endswith(".json"):
            messages.error(request, "Invalid file extension. Please upload a .json file.")
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
            return redirect(f"/generate/?semester_id={semester.id}")
        except ValidationError as ve:
            error_details = ve.message if hasattr(ve, "message") else str(ve)
            messages.error(request, f"JSON Validation Error:\n{error_details}")
        except Exception as e:
            messages.error(request, f"Import Error: {str(e)}")

    return render(request, "scheduler/upload_json.html")


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

    # Build the Day x Period Grid
    # Get all distinct periods and working days present in TimeSlot
    time_slots = list(TimeSlot.objects.all().order_by("day", "period_number"))
    
    # Days present in slots
    day_keys_present = []
    for d_key, d_label in DAY_CHOICES:
        if any(ts.day == d_key for ts in time_slots):
            day_keys_present.append((d_key, d_label))

    # Distinct period numbers and their typical time strings
    period_numbers = sorted(list({ts.period_number for ts in time_slots}))
    period_info = {}
    for ts in time_slots:
        if ts.period_number not in period_info:
            period_info[ts.period_number] = f"{ts.start_time.strftime('%H:%M')} - {ts.end_time.strftime('%H:%M')}"

    # Load entries for this semester and division
    grid = {}  # period_number -> {day_key: entry}
    for p in period_numbers:
        grid[p] = {d_key: None for d_key, _ in day_keys_present}

    if selected_semester and selected_division:
        entries = (
            TimetableEntry.objects.filter(
                semester=selected_semester,
                assignment__division=selected_division
            )
            .select_related(
                "assignment__subject",
                "assignment__teacher",
                "room",
                "time_slot"
            )
        )
        for entry in entries:
            p_num = entry.time_slot.period_number
            d_key = entry.time_slot.day
            if p_num in grid and d_key in grid[p_num]:
                grid[p_num][d_key] = entry

    # Prepare table rows for template
    grid_rows = []
    for p in period_numbers:
        cells = []
        for d_key, _ in day_keys_present:
            cells.append({
                "day": d_key,
                "entry": grid[p].get(d_key)
            })
        grid_rows.append({
            "period_number": p,
            "time_range": period_info.get(p, ""),
            "cells": cells
        })

    context = {
        "semesters": semesters,
        "selected_semester": selected_semester,
        "divisions": divisions,
        "selected_division": selected_division,
        "day_headers": [d_label for _, d_label in day_keys_present],
        "grid_rows": grid_rows,
    }
    return render(request, "scheduler/timetable_view.html", context)
