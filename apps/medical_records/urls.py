from django.urls import path
from . import views

app_name = 'medical_records'

urlpatterns = [
    path('', views.record_list, name='list'),
    # Before the <uuid:pk> route: a literal segment must not be a candidate pk.
    path('<uuid:pk>/', views.record_detail, name='detail'),
    path('<uuid:pk>/delete/', views.record_delete, name='delete'),
    path('upload/kanta/', views.upload_kanta, name='upload_kanta'),
    path('upload/wearable/', views.upload_wearable, name='upload_wearable'),
    path('upload/pdf/', views.upload_pdf, name='upload_pdf'),
    path('upload/text/', views.upload_text, name='upload_text'),
    path('upload/scan/', views.upload_scan, name='upload_scan'),
    path('upload/scan/ocr/', views.scan_ocr, name='scan_ocr'),
]
