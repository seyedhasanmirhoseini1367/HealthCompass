from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from . import views

app_name = 'api'

urlpatterns = [
    # Auth
    path('auth/register/',      views.register,              name='register'),
    path('auth/login/',         TokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/',       TokenRefreshView.as_view(),    name='token_refresh'),
    path('auth/me/',            views.me,                    name='me'),
    path('auth/forgot-password/', views.forgot_password,     name='forgot_password'),

    # Medical records
    path('records/',            views.records_list,          name='records_list'),
    path('records/<int:pk>/',   views.record_detail,         name='record_detail'),

    # Dashboard
    path('dashboard/',          views.dashboard_summary,     name='dashboard'),

    # AI Assistant
    path('assistant/ask/',      views.assistant_ask,         name='assistant_ask'),
]
