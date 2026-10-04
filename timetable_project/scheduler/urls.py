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

    # Student & teacher personal timetable (read-only)
    path('my-timetable/', views.my_timetable_view, name='my_timetable'),

    # Administrator: full control
    path('upload/', views.upload_json_view, name='upload_json'),
    path('generate/', views.generate_timetable_view, name='generate_timetable'),
    path('timetable/', views.timetable_view, name='timetable_view'),

    # Administrator: manual changes to the published timetable
    path('manage/entries/', views.manage_entries_view, name='manage_entries'),
    path('manage/swap/', views.swap_entries_view, name='swap_entries'),
    path('manage/entries/<int:pk>/move/', views.move_entry_view, name='move_entry'),
    path('manage/entries/<int:pk>/delete/', views.delete_entry_view, name='delete_entry'),
]