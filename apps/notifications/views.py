from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .models import Notification


@login_required
def notification_list(request):
    # list() forces evaluation BEFORE the update, deliberately.
    #
    # A queryset is lazy: it was previously evaluated inside the template, i.e.
    # after the UPDATE had already run, so every notification rendered with
    # is_read=True and the unread styling never appeared once. The page marks
    # notifications read as a side effect of viewing them — the user still needs
    # to see which ones were unread when they arrived.
    notifs = list(Notification.objects.filter(user=request.user).order_by('-created_at'))
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return render(request, 'notifications/list.html', {'notifications': notifs})
