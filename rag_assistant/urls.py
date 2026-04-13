from django.urls import path
from . import views

app_name = 'rag_assistant'

urlpatterns = [
    path('', views.chat_view, name='chat'),
    path('new/', views.new_session, name='new_session'),
    path('send/', views.send_message, name='send_message'),
    path('stream/', views.stream_message, name='stream_message'),
    path('session/<uuid:pk>/history/', views.session_history, name='session_history'),
    path('session/<uuid:pk>/rename/', views.rename_session, name='rename_session'),
    path('session/<uuid:pk>/delete/', views.delete_session, name='delete_session'),
]
