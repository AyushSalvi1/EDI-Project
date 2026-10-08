from django.contrib import admin, messages
from django.contrib.auth.models import Group, User
from django.utils.crypto import get_random_string
from django.utils.html import format_html
from .models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    TeacherUnavailability,
    Assignment,
    Student,
    TimetableEntry,
    TimetableChangeLog,
    SchedulingIssue,
    SolverRun,
)
from .roles import TEACHER_GROUP
from .solver import generate_timetable, TimetableSolverError


@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "get_number_of_weeks", "generate_link")
    actions = ["action_regenerate"]

    def get_number_of_weeks(self, obj):
        return obj.number_of_weeks()
    get_number_of_weeks.short_description = "Weeks"

    def generate_link(self, obj):
        return format_html(
            '<a class="button" style="background:#2563eb;color:white;padding:4px 10px;border-radius:4px;text-decoration:none;" '
            'href="/generate/?semester_id={}">Generate Timetable</a>',
            obj.id
        )
    generate_link.short_description = "Action"

    @admin.action(description="Generate / Regenerate Timetable for selected semester(s)")
    def action_regenerate(self, request, queryset):
        for sem in queryset:
            try:
                res = generate_timetable(sem.id)
                self.message_user(
                    request,
                    f"Generated for '{sem.name}': {res['total_hours_scheduled']}/{res['total_hours_requested']} hours scheduled ({res['issues_count']} issues).",
                    messages.SUCCESS
                )
            except TimetableSolverError as e:
                self.message_user(request, f"Error generating for '{sem.name}': {str(e)}", messages.ERROR)


@admin.register(YearDivision)
class YearDivisionAdmin(admin.ModelAdmin):
    list_display = ("name", "year", "division_number", "division_label", "division_prefix", "strength")
    list_filter = ("year",)
    search_fields = ("name", "division_label", "division_prefix")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_lab")
    list_filter = ("is_lab",)
    search_fields = ("name", "code")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "is_lab", "capacity")
    list_filter = ("is_lab",)
    search_fields = ("name",)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("day", "period_number", "start_time", "end_time")
    list_filter = ("day",)


class TeacherUnavailabilityInline(admin.TabularInline):
    model = TeacherUnavailability
    extra = 1


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ("name", "max_hours_per_week", "login_account")
    list_filter = ("user",)
    search_fields = ("name", "user__username")
    inlines = [TeacherUnavailabilityInline]
    actions = ["action_create_login_accounts"]
    autocomplete_fields = ("user",)

    @admin.display(description="Login Account")
    def login_account(self, obj):
        if obj.user:
            return format_html(
                '<span style="color:#059669;font-weight:600;">{}</span>', obj.user.username
            )
        return format_html('<span style="color:#dc2626;">No login yet</span>')

    @admin.action(description="Create portal login accounts for selected teachers")
    def action_create_login_accounts(self, request, queryset):
        group, _ = Group.objects.get_or_create(name=TEACHER_GROUP)
        created, skipped = [], []
        for teacher in queryset:
            if teacher.user:
                skipped.append(teacher.name)
                continue
            base = teacher.name.split()[-1].lower() or f"teacher{teacher.pk}"
            username, suffix = base, 1
            while User.objects.filter(username=username).exists():
                suffix += 1
                username = f"{base}{suffix}"

            temporary_password = get_random_string(12)
            user = User.objects.create_user(
                username=username,
                password=temporary_password,
            )
            user.first_name = teacher.name
            user.is_staff = False
            user.save()
            user.groups.add(group)
            teacher.user = user
            teacher.save(update_fields=["user"])
            created.append(f"{username} / {temporary_password}")

        if created:
            self.message_user(
                request,
                "Created %d teacher login(s). Temporary username / password pairs "
                "(shown once - share them securely and ask the teacher to change the "
                "password after first sign-in): %s" % (len(created), ", ".join(created)),
                messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f"Skipped {len(skipped)} teacher(s) that already had a login.",
                messages.WARNING,
            )


@admin.register(TeacherUnavailability)
class TeacherUnavailabilityAdmin(admin.ModelAdmin):
    list_display = ("teacher", "time_slot")
    list_filter = ("teacher", "time_slot__day")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("division", "subject", "teacher", "semester", "total_hours_for_semester", "get_weekly_hours", "regenerate_btn")
    list_filter = ("semester", "division__year", "teacher", "subject")
    search_fields = ("teacher__name", "subject__name", "division__name")
    actions = ["action_regenerate_semester"]

    def get_weekly_hours(self, obj):
        return f"{obj.weekly_hours()} h/wk"
    get_weekly_hours.short_description = "Weekly Hours"

    def regenerate_btn(self, obj):
        return format_html(
            '<a class="button" style="background:#059669;color:white;padding:3px 8px;border-radius:4px;text-decoration:none;" '
            'href="/generate/?semester_id={}">Regenerate</a>',
            obj.semester_id
        )
    regenerate_btn.short_description = "Regenerate"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Inform the admin and provide direct link to regenerate
        regen_url = f"/generate/?semester_id={obj.semester_id}"
        messages.info(
            request,
            format_html(
                'Assignment updated. <a href="{}" style="font-weight:bold;color:#1d4ed8;text-decoration:underline;">Click here to Regenerate Timetable</a> for {} to apply changes and check for issues.',
                regen_url,
                obj.semester.name
            )
        )

    @admin.action(description="Regenerate Timetable for selected assignments' semester")
    def action_regenerate_semester(self, request, queryset):
        semester_ids = set(queryset.values_list("semester_id", flat=True))
        for s_id in semester_ids:
            try:
                res = generate_timetable(s_id)
                self.message_user(request, res["message"], messages.SUCCESS)
            except TimetableSolverError as e:
                self.message_user(request, f"Solver error: {str(e)}", messages.ERROR)


@admin.register(TimetableEntry)
class TimetableEntryAdmin(admin.ModelAdmin):
    list_display = ("semester", "time_slot", "get_division", "get_subject", "get_teacher", "room")
    list_filter = ("semester", "time_slot__day", "room")
    search_fields = ("assignment__teacher__name", "assignment__subject__name", "assignment__division__name")
    actions = ["action_open_manager"]

    def get_division(self, obj):
        return obj.assignment.division.name
    get_division.short_description = "Division"

    def get_subject(self, obj):
        return obj.assignment.subject.name
    get_subject.short_description = "Subject"

    def get_teacher(self, obj):
        return obj.assignment.teacher.name
    get_teacher.short_description = "Teacher"

    @admin.action(description="Open these semesters in the timetable manager")
    def action_open_manager(self, request, queryset):
        semester_ids = sorted(set(queryset.values_list("semester_id", flat=True)))
        if not semester_ids:
            return
        url = "/manage/entries/?semester_id={}".format(semester_ids[0])
        self.message_user(
            request,
            format_html(
                'Manage, move, swap or remove entries in the <a href="{}" '
                'style="font-weight:bold;color:#1d4ed8;text-decoration:underline;">timetable manager</a>.',
                url,
            ),
            messages.INFO,
        )


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("roll_number", "full_name", "division", "username", "is_active")
    list_filter = ("division", "is_active")
    search_fields = ("roll_number", "full_name", "user__username", "email")
    autocomplete_fields = ("user", "division")

    @admin.display(description="Username")
    def username(self, obj):
        return obj.user.username if obj.user else "-"
    username.short_description = "Username"


@admin.register(SchedulingIssue)
class SchedulingIssueAdmin(admin.ModelAdmin):
    list_display = ("semester", "assignment", "hours_requested", "hours_scheduled", "reason", "suggestion")
    list_filter = ("semester",)
    search_fields = ("assignment__teacher__name", "assignment__subject__name", "reason", "suggestion")


@admin.register(TimetableChangeLog)
class TimetableChangeLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "semester", "action", "changed_by")
    list_filter = ("semester", "action")
    search_fields = ("detail", "changed_by__username")
    readonly_fields = ("created_at", "semester", "changed_by", "action", "detail")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SolverRun)
class SolverRunAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "semester", "solver_status", "wall_time_seconds",
        "variable_count", "constraint_count", "ram_used_after_mb",
    )
    list_filter = ("semester", "solver_status")
    readonly_fields = (
        "created_at", "semester", "cpu_percent_before", "cpu_percent_after",
        "ram_used_before_mb", "ram_used_after_mb", "wall_time_seconds",
        "variable_count", "constraint_count", "solver_status",
    )
