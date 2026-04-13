from django.shortcuts import render
from stories.models import Story


def home(request):
    recent = Story.objects.filter(
        status='published', is_public=True
    ).order_by('-published_at')[:3]
    return render(request, 'home.html', {'recent_stories': recent})
