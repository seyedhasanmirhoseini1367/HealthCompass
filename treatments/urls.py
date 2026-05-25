from django.urls import path
from . import views

app_name = 'treatments'

urlpatterns = [
    path('',                              views.course_list,          name='list'),
    path('create/',                       views.course_create,        name='create'),
    path('<int:pk>/',                     views.course_detail,        name='detail'),
    path('<int:pk>/milestone/add/',       views.add_milestone,        name='add_milestone'),
    path('<int:pk>/milestone/<int:mid>/delete/', views.delete_milestone, name='delete_milestone'),
    path('<int:pk>/monitor/add/',         views.add_monitor,          name='add_monitor'),
    path('<int:pk>/monitor/<int:mid>/check/', views.mark_monitor_checked, name='check_monitor'),
    path('<int:pk>/monitor/<int:mid>/delete/', views.delete_monitor,  name='delete_monitor'),
    path('<int:pk>/status/',              views.set_status,           name='set_status'),
    path('<int:pk>/delete/',              views.delete_course,        name='delete'),
]
