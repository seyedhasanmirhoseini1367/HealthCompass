from django.db import models
from django.conf import settings
from django.utils.text import slugify
from django.utils import timezone


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self): return self.name


class Story(models.Model):
    class Status(models.TextChoices):
        DRAFT     = 'draft',     'Draft'
        PUBLISHED = 'published', 'Published'

    title             = models.CharField(max_length=200)
    slug              = models.SlugField(max_length=220, unique=True, blank=True)
    author            = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                          related_name='stories')
    excerpt           = models.TextField(max_length=500, blank=True)
    content           = models.TextField()
    featured_image    = models.ImageField(upload_to='stories/', blank=True, null=True)
    tags              = models.ManyToManyField(Tag, blank=True)
    status            = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    is_public         = models.BooleanField(default=True)
    view_count        = models.PositiveIntegerField(default=0)
    read_time_minutes = models.PositiveIntegerField(default=1)
    created_at        = models.DateTimeField(auto_now_add=True)
    published_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-published_at', '-created_at']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        if self.status == self.Status.PUBLISHED and not self.published_at:
            self.published_at = timezone.now()
        word_count = len(self.content.split())
        self.read_time_minutes = max(1, round(word_count / 200))
        super().save(*args, **kwargs)

    def __str__(self): return self.title


class Comment(models.Model):
    story   = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='comments')
    author  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                related_name='story_comments')
    parent  = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE,
                related_name='replies')
    body    = models.TextField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Comment by {self.author.username} on {self.story.title}'
