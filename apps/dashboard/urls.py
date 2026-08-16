from django.urls import path
from . import hubs, views

app_name = 'dashboard'

urlpatterns = [
    path('', views.home, name='home'),
    path('health/', hubs.my_health, name='my_health'),
    path('settings/', hubs.settings_hub, name='settings'),
    path('monitoring/', views.monitoring, name='monitoring'),
    path('patient/<int:patient_pk>/records/', views.patient_records, name='patient_records'),
    path('record/<uuid:record_pk>/', views.doctor_record_detail, name='doctor_record'),
    path('links/create/', views.create_link, name='create_link'),
    path('links/<int:pk>/remove/', views.remove_link, name='remove_link'),
]
