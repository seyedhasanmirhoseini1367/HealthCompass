from django.urls import path
from . import views

app_name = 'goals'

urlpatterns = [
    path('',                       views.goal_list,           name='list'),
    path('add/',                   views.add_goal,            name='add'),
    path('<int:pk>/entry/',        views.add_entry,           name='add_entry'),
    path('<int:pk>/status/',       views.update_goal_status,  name='update_status'),
    path('<int:pk>/delete/',       views.delete_goal,         name='delete'),
]
