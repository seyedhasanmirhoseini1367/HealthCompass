from django.contrib import admin
from .models import Story, Tag

@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display  = ('title', 'author', 'status', 'is_public', 'published_at', 'view_count')
    list_filter   = ('status', 'is_public')
    list_editable = ('status', 'is_public')
    prepopulated_fields = {'slug': ('title',)}

admin.site.register(Tag)
