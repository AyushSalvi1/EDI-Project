"""
Role-based access control helpers for the timetable portal.

Three roles exist:
  * Admin   -- full control: import data, run the solver, move/swap/delete entries.
  * Teacher -- read-only view of the timetable of the Teacher record linked to their user.
  * Student -- read-only view of the timetable of the division on their Student profile.

An account is an admin if it is a superuser, has ``is_staff``, or belongs to the
``Administrators`` group. Teacher and student identity is resolved from the
``Teacher.user`` / ``Student.user`` one-to-one links rather than from groups, so
that access can never drift away from the academic record.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

ADMIN_GROUP = "Administrators"
TEACHER_GROUP = "Teachers"
STUDENT_GROUP = "Students"


def is_admin(user):
    """True for superusers, staff members, or members of the Administrators group."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name=ADMIN_GROUP).exists()


def teacher_profile(user):
    """Return the Teacher record linked to this user, or None."""
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "teacher_profile", None)


def student_profile(user):
    """Return the Student record linked to this user, or None."""
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "student_profile", None)


def is_teacher(user):
    """True if this user has a linked Teacher record (admins are handled separately)."""
    return teacher_profile(user) is not None


def is_student(user):
    """True if this user has a linked Student record."""
    return student_profile(user) is not None


def primary_role(user):
    """Human readable role used for navigation and the post-login redirect."""
    if is_admin(user):
        return "admin"
    if is_teacher(user):
        return "teacher"
    if is_student(user):
        return "student"
    return "guest"


def landing_page_for(user):
    """URL name each role should land on after logging in."""
    return "scheduler:home" if is_admin(user) else "scheduler:my_timetable"


def admin_required(view):
    """Restrict a view to administrators; others are bounced to their own timetable."""

    @login_required
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            messages.error(
                request,
                "Administrator access is required for that page. "
                "You are signed in as a limited account.",
            )
            return redirect("scheduler:my_timetable")
        return view(request, *args, **kwargs)

    return _wrapped