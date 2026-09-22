import json
import re
from datetime import datetime
from django.db import transaction
from django.core.exceptions import ValidationError

from .models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    TeacherUnavailability,
    Assignment,
    generate_time_slots,
)


def _validate_date(date_str, field_name):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValidationError(f"Field '{field_name}' must be in YYYY-MM-DD format (got: {date_str}).")


def _validate_time(time_str, field_name):
    try:
        return datetime.strptime(time_str, "%H:%M").time()
    except (ValueError, TypeError):
        raise ValidationError(f"Field '{field_name}' must be in HH:MM format (got: {time_str}).")


def validate_college_data(data: dict):
    """
    Validates the JSON structure with clear error messages for missing/malformed fields
    before touching the database.
    """
    if not isinstance(data, dict):
        raise ValidationError("JSON root must be an object.")

    errors = []

    # 1. College
    if "college" not in data or not isinstance(data["college"], dict):
        errors.append("Missing or invalid 'college' object.")
    else:
        col = data["college"]
        required_col_fields = [
            "semester_name", "start_date", "end_date", "working_days",
            "daily_start_time", "daily_end_time", "period_duration_minutes", "lunch_break"
        ]
        for field in required_col_fields:
            if field not in col:
                errors.append(f"Missing required field in 'college': '{field}'.")

        # Validate dates
        if "start_date" in col and "end_date" in col:
            try:
                start_d = _validate_date(col["start_date"], "start_date")
                end_d = _validate_date(col["end_date"], "end_date")
                if end_d <= start_d:
                    errors.append("'end_date' must be after 'start_date'.")
            except ValidationError as e:
                errors.append(str(e))

        # Validate working days
        valid_days = {"MON", "TUE", "WED", "THU", "FRI", "SAT"}
        if "working_days" in col:
            if not isinstance(col["working_days"], list) or len(col["working_days"]) == 0:
                errors.append("'working_days' must be a non-empty list.")
            else:
                for d in col["working_days"]:
                    if not isinstance(d, str) or d.upper().strip() not in valid_days:
                        errors.append(f"Invalid working day '{d}'. Must be one of {sorted(list(valid_days))}.")

        # Validate times
        if "daily_start_time" in col:
            try:
                _validate_time(col["daily_start_time"], "daily_start_time")
            except ValidationError as e:
                errors.append(str(e))

        if "daily_end_time" in col:
            try:
                _validate_time(col["daily_end_time"], "daily_end_time")
            except ValidationError as e:
                errors.append(str(e))

        if "period_duration_minutes" in col:
            if not isinstance(col["period_duration_minutes"], int) or col["period_duration_minutes"] <= 0:
                errors.append("'period_duration_minutes' must be a positive integer.")

        if "lunch_break" in col:
            lb = col["lunch_break"]
            if not isinstance(lb, dict) or "start_time" not in lb or "end_time" not in lb:
                errors.append("'lunch_break' must be an object with 'start_time' and 'end_time'.")
            else:
                try:
                    lb_s = _validate_time(lb["start_time"], "lunch_break.start_time")
                    lb_e = _validate_time(lb["end_time"], "lunch_break.end_time")
                    if lb_e <= lb_s:
                        errors.append("lunch_break 'end_time' must be after 'start_time'.")
                except ValidationError as e:
                    errors.append(str(e))

    # 2. Years
    year_division_map = {}  # year -> set of division numbers (1-based)
    if "years" not in data or not isinstance(data["years"], list):
        errors.append("Missing or invalid 'years' list.")
    else:
        for idx, y in enumerate(data["years"]):
            if not isinstance(y, dict):
                errors.append(f"Item {idx} in 'years' must be an object.")
                continue
            year_num = y.get("year")
            num_divs = y.get("number_of_divisions")
            strength = y.get("strength_per_division")

            if not isinstance(year_num, int) or not (1 <= year_num <= 4):
                errors.append(f"In 'years'[{idx}]: 'year' must be an integer between 1 and 4.")
            if not isinstance(num_divs, int) or num_divs <= 0:
                errors.append(f"In 'years'[{idx}]: 'number_of_divisions' must be a positive integer.")
            if not isinstance(strength, int) or strength <= 0:
                errors.append(f"In 'years'[{idx}]: 'strength_per_division' must be a positive integer.")

            if isinstance(year_num, int) and isinstance(num_divs, int) and num_divs > 0:
                year_division_map[year_num] = set(range(1, num_divs + 1))

    # 3. Subjects
    subject_ids = set()
    if "subjects" not in data or not isinstance(data["subjects"], list):
        errors.append("Missing or invalid 'subjects' list.")
    else:
        for idx, s in enumerate(data["subjects"]):
            if not isinstance(s, dict):
                errors.append(f"Item {idx} in 'subjects' must be an object.")
                continue
            s_id = s.get("id")
            s_name = s.get("name")
            is_lab = s.get("is_lab")

            if not s_id or not isinstance(s_id, str):
                errors.append(f"In 'subjects'[{idx}]: 'id' must be a non-empty string.")
            elif s_id in subject_ids:
                errors.append(f"Duplicate subject id '{s_id}'.")
            else:
                subject_ids.add(s_id)

            if not s_name or not isinstance(s_name, str):
                errors.append(f"In 'subjects'[{idx}]: 'name' must be a non-empty string.")
            if not isinstance(is_lab, bool):
                errors.append(f"In 'subjects'[{idx}]: 'is_lab' must be a boolean.")

    # 4. Rooms
    if "rooms" not in data or not isinstance(data["rooms"], dict):
        errors.append("Missing or invalid 'rooms' object.")
    else:
        r = data["rooms"]
        for key in ["number_of_regular_rooms", "regular_room_capacity", "number_of_lab_rooms", "lab_room_capacity"]:
            if key not in r:
                errors.append(f"Missing required field in 'rooms': '{key}'.")
            elif not isinstance(r[key], int) or r[key] < 0:
                errors.append(f"Field '{key}' in 'rooms' must be a non-negative integer.")

    # 5. Teachers
    if "teachers" not in data or not isinstance(data["teachers"], list):
        errors.append("Missing or invalid 'teachers' list.")
    else:
        slot_pattern = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT)-P(\d+)$", re.IGNORECASE)
        for idx, t in enumerate(data["teachers"]):
            if not isinstance(t, dict):
                errors.append(f"Item {idx} in 'teachers' must be an object.")
                continue
            t_name = t.get("name")
            max_hours = t.get("max_hours_per_week")

            if not t_name or not isinstance(t_name, str):
                errors.append(f"In 'teachers'[{idx}]: 'name' must be a non-empty string.")
            if not isinstance(max_hours, int) or max_hours <= 0:
                errors.append(f"In 'teachers'[{idx}]: 'max_hours_per_week' must be a positive integer.")

            # Validate unavailable slots format e.g. "FRI-P4"
            unavail = t.get("unavailable_slots", [])
            if not isinstance(unavail, list):
                errors.append(f"In 'teachers'[{idx}]: 'unavailable_slots' must be a list.")
            else:
                for slot_str in unavail:
                    if not isinstance(slot_str, str) or not slot_pattern.match(slot_str.strip()):
                        errors.append(f"Invalid unavailable slot '{slot_str}' for teacher '{t_name}'. Must be format 'DAY-P#' (e.g. 'FRI-P4').")

            # Validate allocations
            allocs = t.get("allocations", [])
            if not isinstance(allocs, list):
                errors.append(f"In 'teachers'[{idx}]: 'allocations' must be a list.")
            else:
                for a_idx, a in enumerate(allocs):
                    if not isinstance(a, dict):
                        errors.append(f"Teacher '{t_name}' allocation {a_idx} must be an object.")
                        continue
                    a_year = a.get("year")
                    a_div = a.get("division")
                    a_subj = a.get("subject_id")
                    a_hours = a.get("total_hours_for_semester")

                    if a_year not in year_division_map:
                        errors.append(f"Teacher '{t_name}' allocation references undefined year '{a_year}'.")
                    elif a_div not in year_division_map[a_year]:
                        errors.append(f"Teacher '{t_name}' allocation references invalid division '{a_div}' for year '{a_year}'.")

                    if a_subj not in subject_ids:
                        errors.append(f"Teacher '{t_name}' allocation references undefined subject_id '{a_subj}'.")

                    if not isinstance(a_hours, int) or a_hours <= 0:
                        errors.append(f"Teacher '{t_name}' allocation 'total_hours_for_semester' must be a positive integer.")

    if errors:
        raise ValidationError("\n".join(errors))


def import_college_data_from_dict(data: dict) -> dict:
    """
    Parses validated JSON dictionary and populates all database tables
    inside an atomic transaction.
    """
    validate_college_data(data)

    with transaction.atomic():
        college = data["college"]
        start_d = _validate_date(college["start_date"], "start_date")
        end_d = _validate_date(college["end_date"], "end_date")

        # 1. Create Semester
        semester = Semester.objects.create(
            name=college.get("semester_name", f"{college.get('name', 'College')} Semester"),
            start_date=start_d,
            end_date=end_d
        )

        # 2. Generate TimeSlots
        created_slots = generate_time_slots(
            working_days=college["working_days"],
            daily_start_time=college["daily_start_time"],
            daily_end_time=college["daily_end_time"],
            period_duration_minutes=college["period_duration_minutes"],
            lunch_break=college["lunch_break"]
        )

        # Slot lookup: (day, period_number) -> TimeSlot
        slot_map = {(s.day.upper(), s.period_number): s for s in TimeSlot.objects.all()}

        # 3. Generate YearDivisions
        division_lookup = {}  # (year, division_num) -> YearDivision
        created_divisions = []
        for y_entry in data["years"]:
            divs = YearDivision.generate_for_year(
                year=y_entry["year"],
                number_of_divisions=y_entry["number_of_divisions"],
                strength=y_entry["strength_per_division"]
            )
            for local_division_number, d in enumerate(divs, start=1):
                division_lookup[(d.year, local_division_number)] = d
                created_divisions.append(d)

        # 4. Create Subjects
        subject_lookup = {}  # subject_id -> Subject
        created_subjects = []
        for s_entry in data["subjects"]:
            s_obj, _ = Subject.objects.get_or_create(
                code=s_entry["id"],
                defaults={
                    "name": s_entry["name"],
                    "is_lab": s_entry["is_lab"]
                }
            )
            subject_lookup[s_entry["id"]] = s_obj
            created_subjects.append(s_obj)

        # 5. Generate Rooms
        rooms_cfg = data["rooms"]
        created_rooms = Room.generate_rooms(
            number_of_regular_rooms=rooms_cfg["number_of_regular_rooms"],
            regular_room_capacity=rooms_cfg["regular_room_capacity"],
            number_of_lab_rooms=rooms_cfg["number_of_lab_rooms"],
            lab_room_capacity=rooms_cfg["lab_room_capacity"]
        )

        # 6. Create Teachers, TeacherUnavailability, and Assignments
        created_teachers = []
        created_unavailabilities = []
        created_assignments = []

        slot_pattern = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT)-P(\d+)$", re.IGNORECASE)

        for t_entry in data["teachers"]:
            teacher = Teacher.objects.create(
                name=t_entry["name"],
                max_hours_per_week=t_entry.get("max_hours_per_week", 24)
            )
            created_teachers.append(teacher)

            # Unavailabilities
            for slot_str in t_entry.get("unavailable_slots", []):
                m = slot_pattern.match(slot_str.strip())
                if m:
                    d_code = m.group(1).upper()
                    p_num = int(m.group(2))
                    ts = slot_map.get((d_code, p_num))
                    if ts:
                        unavail = TeacherUnavailability.objects.create(
                            teacher=teacher,
                            time_slot=ts
                        )
                        created_unavailabilities.append(unavail)

            # Allocations -> Assignments
            for a_entry in t_entry.get("allocations", []):
                div_obj = division_lookup.get((a_entry["year"], a_entry["division"]))
                subj_obj = subject_lookup.get(a_entry["subject_id"])
                if not div_obj:
                    raise ValidationError(
                        f"Teacher '{t_entry['name']}' allocation references "
                        f"unresolved division {a_entry['year']}-{a_entry['division']}."
                    )
                if not subj_obj:
                    raise ValidationError(
                        f"Teacher '{t_entry['name']}' allocation references "
                        f"unresolved subject_id '{a_entry['subject_id']}'."
                    )

                assignment = Assignment.objects.create(
                    semester=semester,
                    teacher=teacher,
                    subject=subj_obj,
                    division=div_obj,
                    total_hours_for_semester=a_entry["total_hours_for_semester"]
                )
                created_assignments.append(assignment)

        return {
            "semester": semester,
            "divisions_count": len(created_divisions),
            "subjects_count": len(created_subjects),
            "rooms_count": len(created_rooms),
            "teachers_count": len(created_teachers),
            "assignments_count": len(created_assignments),
            "slots_count": len(created_slots),
        }


def import_college_data_from_json(file_or_path) -> dict:
    """
    Accepts a filepath, open file object, or string containing JSON,
    and runs the full validation and import pipeline.
    """
    if hasattr(file_or_path, "read"):
        content = file_or_path.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        data = json.loads(content)
    elif isinstance(file_or_path, str):
        if file_or_path.strip().startswith("{"):
            data = json.loads(file_or_path)
        else:
            with open(file_or_path, "r", encoding="utf-8") as f:
                data = json.load(f)
    else:
        raise ValidationError("Invalid input for JSON import. Expected file path, file object, or JSON string.")

    return import_college_data_from_dict(data)
