from django.urls import path
from . import views

app_name = 'ai_insights'

urlpatterns = [
    path('', views.model_list, name='list'),
    path('health/', views.health_view, name='health'),
    path('population/', views.population_view, name='population'),
    path('models/', views.models_view, name='models'),
    path('analytics/', views.patient_analytics, name='patient_analytics'),
    path('submit/', views.submit_model, name='submit_model'),
    path('my-models/', views.my_models, name='my_models'),
    path('my-predictions/', views.my_predictions, name='my_predictions'),
    path('prediction/<uuid:pk>/', views.prediction_detail, name='prediction_detail'),
    path('seizure/', views.seizure_analysis, name='seizure_analysis'),
    path('seizure-realtime/', views.seizure_realtime, name='seizure_realtime'),
    path('seizure-realtime/load/', views.seizure_realtime_load, name='seizure_realtime_load'),
    path('seizure-realtime/models/', views.seizure_realtime_models, name='seizure_realtime_models'),
    path('seizure-realtime/predict-chunk/', views.seizure_realtime_predict_chunk, name='seizure_realtime_predict_chunk'),
    path('<slug:slug>/', views.model_detail, name='model_detail'),
    path('<slug:slug>/run/', views.run_prediction, name='run_prediction'),
    path('debug/handlers/', views.debug_handlers, name='debug_handlers'),
]
