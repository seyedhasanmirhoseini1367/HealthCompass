from django.urls import path

from . import views

app_name = 'scribe'

urlpatterns = [
    path('',                     views.encounter_list,   name='list'),
    path('upload/',              views.encounter_upload, name='upload'),
    path('<int:pk>/',            views.encounter_detail, name='detail'),
    path('<int:pk>/status/',     views.encounter_status, name='status'),
    path('<int:pk>/edit/',       views.encounter_edit,   name='edit'),
    path('<int:pk>/print/',      views.encounter_print,  name='print'),
    path('<int:pk>/retry/',      views.encounter_retry,  name='retry'),
    path('<int:pk>/delete/',     views.encounter_delete, name='delete'),
]
