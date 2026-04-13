from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .models import Story, Tag, Comment


def story_list(request):
    stories = Story.objects.filter(status='published', is_public=True)
    if request.GET.get('tag'):
        stories = stories.filter(tags__slug=request.GET['tag'])
    return render(request, 'stories/list.html', {'stories': stories, 'tags': Tag.objects.all()})


def story_detail(request, slug):
    story = get_object_or_404(Story, slug=slug, status='published')
    Story.objects.filter(pk=story.pk).update(view_count=story.view_count + 1)
    # Top-level comments only; replies are prefetched via related_name
    comments = story.comments.filter(parent=None).prefetch_related('replies__author')
    return render(request, 'stories/detail.html', {
        'story':    story,
        'comments': comments,
    })


@login_required
@require_POST
def add_comment(request, slug):
    story  = get_object_or_404(Story, slug=slug, status='published')
    body   = request.POST.get('body', '').strip()
    parent_id = request.POST.get('parent_id')
    if body:
        parent = None
        if parent_id:
            try:
                parent = Comment.objects.get(pk=parent_id, story=story)
            except Comment.DoesNotExist:
                pass
        Comment.objects.create(story=story, author=request.user, body=body, parent=parent)
    return redirect('stories:detail', slug=slug)
