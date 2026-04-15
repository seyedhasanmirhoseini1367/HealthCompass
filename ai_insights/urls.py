from django.urls import path
from . import views

app_name = 'ai_insights'

urlpatterns = [
    path('', views.model_list, name='list'),
    path('analytics/', views.patient_analytics, name='patient_analytics'),
    path('submit/', views.submit_model, name='submit_model'),
    path('my-models/', views.my_models, name='my_models'),
    path('my-predictions/', views.my_predictions, name='my_predictions'),
    path('prediction/<uuid:pk>/', views.prediction_detail, name='prediction_detail'),
    path('<slug:slug>/', views.model_detail, name='model_detail'),
    path('<slug:slug>/run/', views.run_prediction, name='run_prediction'),
    path('debug/handlers/', views.debug_handlers, name='debug_handlers'),
]
