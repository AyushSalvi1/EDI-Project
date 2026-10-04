import json

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
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
    Assignment,
    TimetableEntry,
    TimetableChangeLog,
    SchedulingIssue,
    Notification,
    ProposedChange,
    SchedulingPreference,
    DivisionPreference,
    DAY_CHOICES,
    DAY_ORDER,
)
from .edits import delete_entry, move_entry, swap_entries, available_rooms_for
from .importer import import_college_data_from_json, import_college_data_from_dict
from .capacity import analyse_capacity
from .notifications import (
    notify_admins_about_issues,
    notify_admins_about_proposals,
    unread_count,
)
from .preferences import (
    analyse_preference,
    apply_preference,
    describe_week,
    get_or_create_preference,
    save_division_preference,
)
from .proposals import ProposalError, approve_and_apply, reject
from .recommendations import recommend_for_issue, recommend_for_semester
from .pdf_import import (
    ROLE_FIELDS,
    build_payload,
    classify_tables,
    extract_document_title,
    extract_tables,
)
from .pdf_export import (
    build_division_timetable_pdf,
    build_full_institution_pdf,
    build_issues_pdf,
    build_teacher_load_pdf,
)
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
MAX_PDF_UPLOAD_SIZE = 25 * 1024 * 1024
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

    issues = (
        SchedulingIssue.objects.filter(semester=active_semester).count()
        if active_semester else 0
    )
    scheduled = (
        TimetableEntry.objects.filter(semester=active_semester).count()
        if active_semester else 0
    )
    requested = 0
    if active_semester:
        requested = sum(
            assignment.weekly_hours()
            for assignment in Assignment.objects.filter(semester=active_semester)
        )

    context = {
        "semesters": semesters,
        "active_semester": active_semester,
        "total_divisions": YearDivision.objects.count(),
        "total_rooms": Room.objects.count(),
        "total_teachers": Teacher.objects.count(),
        "total_subjects": Subject.objects.count(),
        "total_students": Student.objects.count(),
        "total_entries": scheduled,
        "total_issues": issues,
        "hours_requested": requested,
        "coverage_percent": (
            round(scheduled / requested * 100) if requested else 0
        ),
        "pending_proposals": ProposedChange.objects.filter(status="PENDING").count(),
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

    if not teacher and not student:
        # An administrator has no personal schedule. Notifications link here, so
        # send them to their own landing page instead of raising below.
        messages.info(request, "You do not have a personal timetable.")
        return redirect(reverse("scheduler:home"))

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
            # Tell administrators about anything that could not be scheduled.
            new_notices = notify_admins_about_issues(semester)
            if new_notices:
                messages.warning(
                    request,
                    f"{len(new_notices)} notification(s) sent to administrators "
                    f"about scheduling problems. Review them in the Issue Centre.",
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
def upload_pdf_view(request):
    """
    Two-step PDF import.

    Stage 1 parses the PDF into tables and reports how each table was recognised.
    Stage 2 lets the administrator correct the column mapping and the calendar
    details, then performs exactly the same validated atomic import the JSON
    path uses.
    """
    if request.method == "POST" and request.POST.get("stage") == "import":
        return _pdf_import_stage_two(request)

    tables = []
    document_title = ""
    if request.method == "POST":
        uploaded_file = request.FILES.get("pdf_file")
        if not uploaded_file:
            messages.error(request, "Please choose a PDF file to upload.")
            return render(request, "scheduler/upload_pdf.html")

        if not uploaded_file.name.lower().endswith(".pdf"):
            messages.error(request, "Invalid file type. Please upload a .pdf file.")
            return render(request, "scheduler/upload_pdf.html")

        if uploaded_file.size > MAX_PDF_UPLOAD_SIZE:
            messages.error(request, "The PDF is too large. Maximum allowed size is 25 MB.")
            return render(request, "scheduler/upload_pdf.html")

        try:
            tables = extract_tables(uploaded_file)
            document_title = extract_document_title(uploaded_file)
            uploaded_file.seek(0)
        except Exception as exc:
            messages.error(
                request,
                f"The PDF could not be read: {exc}. If it is a scanned image it must "
                "be OCR'd first, because the importer reads selectable text.",
            )
            return render(request, "scheduler/upload_pdf.html")

        if not tables:
            messages.error(
                request,
                "No tables were found in this PDF. The importer reads ruled tables "
                "with selectable text, not scanned images.",
            )
            return render(request, "scheduler/upload_pdf.html")

        messages.info(
            request,
            f"Found {len(tables)} table(s). Review the column mapping below before importing.",
        )

    return render(
        request,
        "scheduler/upload_pdf.html",
        {
            "tables": tables,
            "tables_json": json.dumps(tables),
            "document_title": document_title,
            "role_fields": ROLE_FIELDS,
        },
    )


def _pdf_import_stage_two(request):
    """Apply the administrator's mapping choices and run the import."""
    raw_tables = request.POST.get("tables_json") or "[]"
    try:
        tables = json.loads(raw_tables)
    except json.JSONDecodeError:
        messages.error(request, "The uploaded table data was corrupted. Please upload the PDF again.")
        return redirect("scheduler:upload_pdf")

    # Re-derive column mappings so a corrected role or a manual column choice is
    # both respected and reflected in the payload that is validated below.
    if any(request.POST.get(f"role_{t.get('index')}") or request.POST.get(f"map_{t.get('index')}_teacher")
           for t in tables):
        tables = classify_tables(tables)
    for table in tables:
        index = str(table.get("index"))
        chosen_role = request.POST.get(f"role_{index}")
        if chosen_role:
            table["role"] = chosen_role
        for field in ROLE_FIELDS.get(table.get("role", ""), {}):
            raw_value = request.POST.get(f"map_{index}_{field}")
            if raw_value not in (None, ""):
                table["mapping"][field] = int(raw_value)

    overrides = {
        key: request.POST.get(key, "").strip()
        for key in (
            "college_name", "semester_name", "start_date", "end_date",
            "working_days", "daily_start_time", "daily_end_time",
            "period_duration_minutes", "lunch_start", "lunch_end",
            "default_strength", "default_max_hours",
            "number_of_regular_rooms", "regular_room_capacity",
            "number_of_lab_rooms", "lab_room_capacity",
        )
    }
    overrides = {k: v for k, v in overrides.items() if v}

    try:
        payload = build_payload(tables, overrides=overrides)
    except ValueError as exc:
        messages.error(request, str(exc))
        return render(
            request,
            "scheduler/upload_pdf.html",
            {"tables": tables, "tables_json": raw_tables,
             "document_title": "", "role_fields": ROLE_FIELDS},
        )

    for warning in payload.pop("_warnings", []):
        messages.warning(request, warning)

    try:
        result = import_college_data_from_dict(payload)
    except ValidationError as exc:
        messages.error(request, f"PDF data failed validation:\n{exc}")
        return render(
            request,
            "scheduler/upload_pdf.html",
            {"tables": tables, "tables_json": raw_tables,
             "document_title": "", "role_fields": ROLE_FIELDS},
        )
    except TimetableSolverError as exc:
        messages.error(request, str(exc))
        return redirect("scheduler:upload_pdf")
    except Exception as exc:
        messages.error(request, f"PDF import failed unexpectedly: {exc}")
        return redirect("scheduler:upload_pdf")

    semester = result["semester"]
    for warning in result.get("warnings", []):
        messages.warning(request, warning)

    messages.success(
        request,
        f"Successfully imported '{semester.name}' from PDF: {result['divisions_count']} divisions, "
        f"{result['subjects_count']} subjects, {result['rooms_count']} rooms, "
        f"{result['teachers_count']} teachers, {result['assignments_count']} assignments.",
    )
    return redirect(reverse("scheduler:generate_timetable") + f"?semester_id={semester.id}")


@admin_required
def capacity_report_view(request):
    """Explain whether the college is physically schedulable before solving."""
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id")
    if semester_id:
        semester = get_object_or_404(Semester, pk=semester_id)
    else:
        semester = semesters.first()

    plan = analyse_capacity(semester) if semester else None
    return render(
        request,
        "scheduler/capacity.html",
        {"semesters": semesters, "selected_semester": semester, "plan": plan},
    )


def _pdf_response(buffer, filename):
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@admin_required
def export_division_pdf_view(request, division_id):
    division = get_object_or_404(YearDivision, pk=division_id)
    semester_id = request.GET.get("semester_id")
    semester = (get_object_or_404(Semester, pk=semester_id)
                if semester_id else Semester.objects.order_by("-start_date").first())
    if semester is None:
        messages.error(request, "Create a semester first by importing college data.")
        return redirect("scheduler:upload_json")

    safe_name = division.name.replace(" ", "_").replace("/", "-")
    return _pdf_response(
        build_division_timetable_pdf(semester, division),
        f"{safe_name}_timetable.pdf",
    )


@admin_required
def export_full_pdf_view(request):
    semester_id = request.GET.get("semester_id")
    semester = (get_object_or_404(Semester, pk=semester_id)
                if semester_id else Semester.objects.order_by("-start_date").first())
    if semester is None:
        messages.error(request, "Create a semester first by importing college data.")
        return redirect("scheduler:upload_json")

    return _pdf_response(
        build_full_institution_pdf(semester),
        f"{semester.name.replace(' ', '_')}_complete_timetable.pdf",
    )


@admin_required
def export_faculty_pdf_view(request):
    semester_id = request.GET.get("semester_id")
    semester = (get_object_or_404(Semester, pk=semester_id)
                if semester_id else Semester.objects.order_by("-start_date").first())
    if semester is None:
        messages.error(request, "Create a semester first by importing college data.")
        return redirect("scheduler:upload_json")

    return _pdf_response(
        build_teacher_load_pdf(semester),
        f"{semester.name.replace(' ', '_')}_faculty_load.pdf",
    )


@admin_required
def export_issues_pdf_view(request):
    semester_id = request.GET.get("semester_id")
    semester = (get_object_or_404(Semester, pk=semester_id)
                if semester_id else Semester.objects.order_by("-start_date").first())
    if semester is None:
        messages.error(request, "Create a semester first by importing college data.")
        return redirect("scheduler:upload_json")

    return _pdf_response(
        build_issues_pdf(semester),
        f"{semester.name.replace(' ', '_')}_scheduling_issues.pdf",
    )


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

# ===========================================================================
# Administrator: issues, recommendations and approval
# ===========================================================================
def _selected_semester(request):
    """Resolve the semester from the query string or form, defaulting to newest."""
    semesters = Semester.objects.all().order_by("-start_date")
    semester_id = request.GET.get("semester_id") or request.POST.get("semester_id")
    if semester_id:
        return semesters, get_object_or_404(Semester, pk=semester_id)
    return semesters, semesters.first()


@admin_required
def issue_centre_view(request):
    """
    Everything the solver could not schedule, with a way to fix each one.

    Nothing here changes the timetable. The page exists so an administrator can
    see the whole problem list in one place and decide what to do about it.
    """
    semesters, semester = _selected_semester(request)

    issues = []
    if semester:
        issues = list(
            SchedulingIssue.objects.filter(semester=semester)
            .select_related(
                "assignment__division", "assignment__subject", "assignment__teacher"
            )
            .prefetch_related("proposals")
        )

    proposals = (
        ProposedChange.objects.filter(semester=semester)
        .select_related("issue", "reviewed_by")
        if semester else ProposedChange.objects.none()
    )

    hours_requested = sum(i.hours_requested for i in issues)
    hours_scheduled = sum(i.hours_scheduled for i in issues)
    entries = (
        TimetableEntry.objects.filter(semester=semester).count() if semester else 0
    )

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "issues": issues,
        "proposals": proposals,
        "pending_proposals": proposals.filter(status="PENDING").count(),
        "hours_requested": hours_requested,
        "hours_scheduled": hours_scheduled,
        "entries_count": entries,
        "recovery_percent": (
            round(hours_scheduled / hours_requested * 100) if hours_requested else 100
        ),
        "unread_count": unread_count(request.user),
    }
    return render(request, "scheduler/issue_centre.html", context)


@admin_required
def find_fixes_view(request):
    """Ask the recommendation engine to propose remedies for every issue."""
    if request.method != "POST":
        return redirect("scheduler:issue_centre")

    semesters, semester = _selected_semester(request)
    if semester is None:
        messages.error(request, "Import college data before looking for fixes.")
        return redirect("scheduler:upload_pdf")

    issue_id = request.POST.get("issue_id")
    query = {"semester_id": semester.id}
    if issue_id:
        query["issue_id"] = issue_id

    try:
        if issue_id:
            issue = get_object_or_404(
                SchedulingIssue.objects.select_related(
                    "assignment__division", "assignment__subject", "assignment__teacher"
                ),
                pk=issue_id,
            )
            proposals = []
            for proposal in recommend_for_issue(issue)[:3]:
                proposal.save()
                proposals.append(proposal)
        else:
            proposals = recommend_for_semester(semester)
    except Exception as exc:
        messages.error(request, f"Could not analyse the timetable: {exc}")
        return redirect(reverse("scheduler:issue_centre") + f"?semester_id={semester.id}")

    notify_admins_about_proposals(proposals)

    if proposals:
        proven = sum(1 for p in proposals if p.verified_gain_hours)
        messages.success(
            request,
            f"Found {len(proposals)} suggested fix(es)"
            + (f", {proven} of which were verified to recover hours."
               if proven else ". None could be verified in a sandbox trial."),
        )
    else:
        messages.info(
            request,
            "No fixes are needed, or every issue already has a fix waiting for review.",
        )

    url = reverse("scheduler:issue_centre") + f"?semester_id={semester.id}"
    if issue_id:
        url += f"&issue_id={issue_id}"
    return redirect(url)


@admin_required
def proposal_list_view(request):
    """Every recommendation, pending or decided, for the selected semester."""
    semesters, semester = _selected_semester(request)

    queryset = ProposedChange.objects.select_related(
        "semester", "issue__assignment__division", "issue__assignment__subject", "reviewed_by"
    )
    if semester:
        queryset = queryset.filter(semester=semester)

    status = request.GET.get("status") or "PENDING"
    if status != "ALL":
        queryset = queryset.filter(status=status)

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "proposals": queryset[:100],
        "status": status,
        "statuses": ProposedChange.STATUS_CHOICES,
        "pending_count": ProposedChange.objects.filter(status="PENDING").count(),
    }
    return render(request, "scheduler/proposals.html", context)


@admin_required
def approve_proposal_view(request, pk):
    """Approve a recommendation, apply it, and notify whoever is affected."""
    proposal = get_object_or_404(
        ProposedChange.objects.select_related("semester", "issue"), pk=pk
    )
    if request.method != "POST":
        return redirect(reverse("scheduler:proposal_list") + f"?semester_id={proposal.semester.id}")

    try:
        outcome = approve_and_apply(proposal, request.user)
    except ProposalError as exc:
        messages.error(request, f"That change could not be applied: {exc}")
    else:
        if outcome["recovered_hours"] > 0:
            messages.success(
                request,
                f"Approved. {outcome['summary']} Recovered "
                f"{outcome['recovered_hours']} hour(s)/week, and everyone affected "
                f"has been notified.",
            )
        elif outcome["status"] == "FAILED":
            messages.warning(
                request,
                f"Approved, but it did not recover any hours. {outcome['summary']} "
                f"Open issues went from {outcome['issues_before']} to "
                f"{outcome['issues_after']}.",
            )
        else:
            messages.warning(
                request,
                f"Approved. {outcome['summary']} No hours changed, but "
                f"{outcome['issues_after']} issue(s) remain.",
            )

    back = _safe_redirect_target(request, request.POST.get("next"))
    if back is None:
        back = reverse("scheduler:proposal_list")
    if "semester_id=" not in back:
        back += ("&" if "?" in back else "?") + f"semester_id={proposal.semester.id}"
    return redirect(back)


@admin_required
def reject_proposal_view(request, pk):
    """Decline a recommendation, leaving the timetable untouched."""
    proposal = get_object_or_404(ProposedChange.objects.select_related("semester"), pk=pk)
    if request.method != "POST":
        return redirect(reverse("scheduler:proposal_list") + f"?semester_id={proposal.semester.id}")

    note = (request.POST.get("note") or "").strip()
    try:
        reject(proposal, request.user, note)
        messages.info(request, f"Declined: {proposal.title}")
    except ProposalError as exc:
        messages.error(request, str(exc))

    return redirect(
        reverse("scheduler:proposal_list") + f"?semester_id={proposal.semester.id}"
    )


# ===========================================================================
# Administrator: notifications
# ===========================================================================
@login_required
def notification_list_view(request):
    """The signed-in user's inbox, with a mark-as-read control."""
    queryset = Notification.objects.filter(recipient=request.user).select_related(
        "semester", "proposal"
    )
    if request.GET.get("filter") == "unread":
        queryset = queryset.filter(is_read=False)

    if request.method == "POST" and request.POST.get("action") == "mark_all_read":
        queryset.update(is_read=True)
        messages.success(request, "All notifications marked as read.")
        return redirect("scheduler:notifications")

    paginator = Paginator(queryset, 25)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "scheduler/notifications.html",
        {
            "page": page,
            "unread_count": queryset.filter(is_read=False).count(),
            "only_unread": request.GET.get("filter") == "unread",
        },
    )


@login_required
def mark_notification_read_view(request, pk):
    """Mark one notification read, optionally following its link."""
    notification = get_object_or_404(
        Notification, pk=pk, recipient=request.user
    )
    notification.is_read = True
    notification.save(update_fields=["is_read"])

    target = notification.url or reverse("scheduler:notifications")
    if not target.startswith("/"):
        target = reverse("scheduler:notifications")
    return redirect(target)


@login_required
def mark_all_read_view(request):
    """Clear the whole inbox for the signed-in user."""
    if request.method != "POST":
        return redirect("scheduler:notifications")
    count = Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True
    )
    messages.success(request, f"Marked {count} notification(s) as read.")
    return redirect("scheduler:notifications")


# ===========================================================================
# Administrator: timetable preferences
# ===========================================================================
@admin_required
def preferences_view(request):
    """
    Set the shape of the week: working days, timings, period length and lunch.

    The page analyses the choice before it is saved, so a week shape that cannot
    hold the curriculum is refused with the reason rather than applied blindly.
    """
    semesters, semester = _selected_semester(request)
    if semester is None:
        messages.error(request, "Import college data first.")
        return redirect("scheduler:upload_pdf")

    preference = get_or_create_preference(semester)
    analysis = None
    division_preferences = list(
        DivisionPreference.objects.filter(
            division__assignments__semester=semester
        ).select_related("division").distinct()
    )

    if request.method == "POST":
        form = {
            "working_days": ",".join(request.POST.getlist("working_days")),
            "day_start_time": request.POST.get("day_start_time") or "",
            "day_end_time": request.POST.get("day_end_time") or "",
            "period_duration_minutes": request.POST.get("period_duration_minutes") or "",
            "lunch_start_time": request.POST.get("lunch_start_time") or "",
            "lunch_end_time": request.POST.get("lunch_end_time") or "",
        }
        try:
            candidate = SchedulingPreference(semester=semester, **{
                key: _coerce_preference_value(key, value)
                for key, value in form.items()
            })
            candidate.validate()
        except ValidationError as exc:
            messages.error(request, "\n".join(exc.messages))
            analysis = None
        else:
            analysis = analyse_preference(semester, candidate)
            blocking = [
                f for f in analysis["findings"] if f["severity"] == "blocking"
            ]
            if blocking and not request.POST.get("force"):
                messages.error(
                    request,
                    "This week shape does not work yet:\n"
                    + "\n".join(f["message"] for f in blocking)
                    + "\n\nTick 'Apply anyway' to proceed despite this.",
                )
            elif request.POST.get("action") == "apply":
                outcome = apply_preference(candidate, request.user)
                result = outcome["result"]
                messages.success(
                    request,
                    f"Timetable preferences applied. "
                    f"{result['total_hours_scheduled']}/"
                    f"{result['total_hours_requested']} hours scheduled, and "
                    f"everyone affected has been notified.",
                )
                if outcome["collateral_semesters"]:
                    names = ", ".join(
                        s.name for s in outcome["collateral_semesters"][:3]
                    )
                    messages.warning(
                        request,
                        f"Heads up: the period grid is shared, so these other "
                        f"semesters lost {outcome['removed_slots']} period(s) and "
                        f"should be regenerated: {names}.",
                    )
                return redirect(
                    reverse("scheduler:preferences") + f"?semester_id={semester.id}"
                )
            else:
                messages.info(
                    request,
                    "This week shape looks workable. Choose 'Apply preferences' "
                    "to rebuild the period grid.",
                )

    else:
        analysis = analyse_preference(semester, preference)

    context = {
        "semesters": semesters,
        "selected_semester": semester,
        "preference": preference,
        "analysis": analysis,
        "week": describe_week(preference) if analysis is None else analysis["summary"],
        "day_choices": DAY_CHOICES,
        "division_preferences": division_preferences,
        "divisions": YearDivision.objects.filter(
            assignments__semester=semester
        ).distinct().order_by("year", "division_number"),
    }
    return render(request, "scheduler/preferences.html", context)


def _coerce_preference_value(key, value):
    """Turn posted strings into the field types SchedulingPreference expects."""
    if key in ("day_start_time", "day_end_time", "lunch_start_time", "lunch_end_time"):
        from datetime import time as _time
        try:
            hours, minutes = str(value).strip().split(":")
            return _time(int(hours), int(minutes))
        except (AttributeError, TypeError, ValueError):
            # A blank or half-typed field must read as a form error, not a 500.
            raise ValidationError(
                f"'{key.replace('_', ' ').title()}' must be a time such as 09:00."
            )
    if key == "period_duration_minutes":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "'Period Duration Minutes' must be a whole number of minutes."
            )
    return value


@admin_required
def division_preference_view(request, division_id):
    """Set one division's own working days and period range."""
    division = get_object_or_404(YearDivision, pk=division_id)
    preference, _created = DivisionPreference.objects.get_or_create(division=division)

    if request.method == "POST":
        if request.POST.get("action") == "clear":
            preference.delete()
            messages.success(
                request,
                f"{division.name} now follows the college-wide timetable.",
            )
            return redirect(
                reverse("scheduler:preferences")
                + f"?semester_id={request.POST.get('semester_id') or ''}"
            )

        days = [
            d.strip().upper()[:3]
            for d in (request.POST.get("working_days") or "").split(",")
            if d.strip()
        ]
        try:
            save_division_preference(division, {
                "working_days": days,
                "first_period": request.POST.get("first_period") or 1,
                "last_period": request.POST.get("last_period") or 0,
                "note": request.POST.get("note") or "",
            })
        except ValidationError as exc:
            messages.error(request, "\n".join(exc.messages))
        else:
            messages.success(
                request,
                f"{division.name}'s preferred slots have been saved. They take "
                f"effect the next time the timetable is generated.",
            )
        return redirect(
            reverse("scheduler:preferences")
            + f"?semester_id={request.POST.get('semester_id') or ''}"
        )

    slots = TimeSlot.objects.all().order_by("day", "period_number")
    return render(
        request,
        "scheduler/division_preference.html",
        {
            "division": division,
            "preference": preference,
            "slots": slots,
            "day_choices": DAY_CHOICES,
            "allowed_count": len(preference.allowed_slots(slots)),
        },
    )
