from django.contrib import admin
from django.utils import timezone
from .models import AIModel, ModelPrediction, HealthAlert


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    list_display  = ('name', 'data_scientist', 'category', 'input_type', 'handler_slug', 'status', 'run_count', 'created_at')
    list_filter   = ('status', 'category', 'input_type')
    list_editable = ('status',)
    search_fields = ('name', 'data_scientist__username', 'description', 'handler_slug')
    actions       = ['approve_models', 'activate_models', 'reject_models']
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'slug', 'data_scientist', 'category', 'description', 'status'),
        }),
        ('Model File & Input', {
            'fields': ('input_type', 'model_file', 'input_schema', 'output_schema'),
        }),
        ('Inference Handler', {
            'fields': ('handler_slug', 'handler_config'),
            'description': (
                'Set handler_slug to dispatch to a custom inference pipeline '
                '(e.g. "eeg_csv", "image_classifier", "tabular_passthrough"). '
                'Leave blank to use the built-in generic runner. '
                'See ai_insights/inference/ADMIN_CONFIGS.md for copy-paste config examples.'
            ),
        }),
        ('AI Interpretation', {
            'fields': ('interpretation_guide',),
        }),
        ('Review', {
            'fields': ('reviewed_by', 'reviewed_at'),
        }),
    )

    @admin.action(description='✅ Approve selected models')
    def approve_models(self, request, queryset):
        count = queryset.filter(status='pending').update(
            status='approved',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f'{count} model(s) approved.')

    @admin.action(description='🟢 Set selected models to Active')
    def activate_models(self, request, queryset):
        count = queryset.update(
            status='active',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f'{count} model(s) activated.')

    @admin.action(description='❌ Reject selected models')
    def reject_models(self, request, queryset):
        count = queryset.update(status='rejected')
        self.message_user(request, f'{count} model(s) rejected.')


@admin.register(HealthAlert)
class HealthAlertAdmin(admin.ModelAdmin):
    list_display  = ('title', 'patient', 'severity', 'is_read', 'created_at')
    list_filter   = ('severity', 'is_read')
    search_fields = ('title', 'patient__username')
    actions       = ['mark_read']

    @admin.action(description='Mark selected alerts as read')
    def mark_read(self, request, queryset):
        queryset.update(is_read=True)


@admin.register(ModelPrediction)
class ModelPredictionAdmin(admin.ModelAdmin):
    list_display = ('model', 'patient', 'risk_score', 'created_at')
    list_filter  = ('model__category',)
    readonly_fields = ('input_data', 'result')
