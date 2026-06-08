from django.urls import path
from . import views

app_name = 'appointments'

urlpatterns = [
    path('',                      views.appointment_list, name='list'),
    path('add/',                  views.add_appointment,  name='add'),
    path('<int:pk>/edit/',        views.edit_appointment, name='edit'),
    path('<int:pk>/delete/',      views.delete_appointment, name='delete'),
    path('<int:pk>/status/',      views.mark_status,      name='mark_status'),
]
