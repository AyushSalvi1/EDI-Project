"""
Template context that every page needs.

Kept in one processor so the navigation badge cannot drift out of sync with the
inbox, and so the unread count costs a single indexed query per page.
"""

from .models import Notification


def navigation_context(request):
    """Expose the signed-in user's unread notification count to every page."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"notifications_unread": 0, "unread_count": 0}

    # One indexed count; the badge appears on every page so it must stay cheap.
    unread = Notification.objects.filter(recipient=user, is_read=False).count()

    return {"notifications_unread": unread, "unread_count": unread}