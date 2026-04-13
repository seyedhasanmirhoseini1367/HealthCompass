from django.urls import path
from . import views

app_name = 'stories'
urlpatterns = [
    path('', views.story_list, name='list'),
    path('<slug:slug>/', views.story_detail, name='detail'),
    path('<slug:slug>/comment/', views.add_comment, name='add_comment'),
]
