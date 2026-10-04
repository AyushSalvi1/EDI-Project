from django.urls import path
from . import views

app_name = 'scheduler'

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('register/', views.register_student_view, name='register'),

    # Role-aware entry point
    path('', views.home_view, name='home'),

    # Notifications (every signed-in user)
    path('notifications/', views.notification_list_view, name='notifications'),
    path('notifications/<int:pk>/read/', views.mark_notification_read_view,
         name='notification_read'),
    path('notifications/read-all/', views.mark_all_read_view, name='notifications_read_all'),

    # Student & teacher personal timetable (read-only)
    path('my-timetable/', views.my_timetable_view, name='my_timetable'),

    # Administrator: full control
    path('upload/', views.upload_json_view, name='upload_json'),
    path('upload/pdf/', views.upload_pdf_view, name='upload_pdf'),
    path('generate/', views.generate_timetable_view, name='generate_timetable'),
    path('timetable/', views.timetable_view, name='timetable_view'),
    path('capacity/', views.capacity_report_view, name='capacity_report'),
    path('preferences/', views.preferences_view, name='preferences'),
    path('preferences/division/<int:division_id>/', views.division_preference_view,
         name='division_preference'),

    # Administrator: issues, recommended fixes and approval
    path('issues/', views.issue_centre_view, name='issue_centre'),
    path('issues/find-fixes/', views.find_fixes_view, name='find_fixes'),
    path('proposals/', views.proposal_list_view, name='proposal_list'),
    path('proposals/<int:pk>/approve/', views.approve_proposal_view, name='approve_proposal'),
    path('proposals/<int:pk>/reject/', views.reject_proposal_view, name='reject_proposal'),

    # Administrator: PDF exports
    path('export/full.pdf', views.export_full_pdf_view, name='export_full_pdf'),
    path('export/faculty.pdf', views.export_faculty_pdf_view, name='export_faculty_pdf'),
    path('export/issues.pdf', views.export_issues_pdf_view, name='export_issues_pdf'),
    path('export/division/<int:division_id>.pdf', views.export_division_pdf_view,
         name='export_division_pdf'),

    # Administrator: manual changes to the published timetable
    path('manage/entries/', views.manage_entries_view, name='manage_entries'),
    path('manage/swap/', views.swap_entries_view, name='swap_entries'),
    path('manage/entries/<int:pk>/move/', views.move_entry_view, name='move_entry'),
    path('manage/entries/<int:pk>/delete/', views.delete_entry_view, name='delete_entry'),
]