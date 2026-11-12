from django.urls import path
from . import views

app_name = 'appointments'

urlpatterns = [
    path('',              views.appointment_list,   name='list'),
    path('calendar/',     views.appointment_month,  name='month'),
    path('<uuid:pk>/calendar.ics', views.appointment_ics, name='ics'),
    path('create/',       views.appointment_create, name='create'),
    path('<uuid:pk>/edit/',   views.appointment_edit,   name='edit'),
    path('<uuid:pk>/delete/', views.appointment_delete, name='delete'),
]
