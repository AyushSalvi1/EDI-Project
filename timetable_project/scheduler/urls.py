from django.urls import path
from . import views

app_name = 'scheduler'

urlpatterns = [
    path('', views.home_view, name='home'),
    path('upload/', views.upload_json_view, name='upload_json'),
    path('generate/', views.generate_timetable_view, name='generate_timetable'),
    path('timetable/', views.timetable_view, name='timetable_view'),
]
