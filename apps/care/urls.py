from django.urls import path

from . import views

app_name = 'care'

urlpatterns = [
    # The Care hub: my reminders, my people, both directions of sharing.
    path('',                      views.my_care,   name='my_care'),
    # Care -> Anna. The canonical page about one person; /accounts/shared/<pk>/
    # redirects here so a caregiver never sees the sharing machinery in a URL.
    path('person/<int:pk>/',      views.person,    name='person'),

    path('respond/<uuid:pk>/',    views.respond,   name='respond'),
    path('tasks/add/',            views.add_task,  name='add_task'),
    path('tasks/<uuid:pk>/stop/', views.stop_task, name='stop_task'),
    path('report/',               views.report,    name='report'),

    # Kept so older links keep working. Both folded into the hub above.
    path('watching/',             views.watching,     name='watching'),
    path('watching/<int:pk>/',    views.watch_detail, name='watch_detail'),
]
