from django.contrib import admin
from django.utils import timezone

from apps.accounts.admin_phi import PhiAccessLoggedAdmin
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
        from apps.accounts.audit import record as record_admin_action
        from apps.accounts.models import AdminAuditEvent

        eligible = list(queryset.filter(status='pending'))
        count = queryset.filter(status='pending').update(
            status='approved',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        for model in eligible:
            record_admin_action(AdminAuditEvent.Action.MODEL_APPROVED,
                                actor=request.user, target=model,
                                target_label=model.slug)
        self.message_user(request, f'{count} model(s) approved.')

    @admin.action(description='🟢 Set selected models to Active')
    def activate_models(self, request, queryset):
        # Only approved models. This used to update the whole selection
        # straight to active, so a model that had never been reviewed could be
        # made patient-facing by selecting it and choosing this action — the
        # approve step was skippable, not merely skipped.
        #
        # The filter is the enforcement HERE because queryset.update() bypasses
        # Model.save() by design, so AIModel._check_activation() cannot see it.
        # Skipped rows are reported rather than silently ignored.
        eligible = queryset.filter(status='approved')
        skipped = queryset.exclude(status='approved').count()

        from apps.accounts.audit import record as record_admin_action
        from apps.accounts.models import AdminAuditEvent

        activated = list(eligible)
        count = eligible.update(
            status='active',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        # Activation is what makes a model patient-facing, so it is the single
        # most consequential administrative action in this system.
        for model in activated:
            record_admin_action(AdminAuditEvent.Action.MODEL_ACTIVATED,
                                actor=request.user, target=model,
                                target_label=model.slug)
        self.message_user(request, f'{count} model(s) activated.')
        if skipped:
            self.message_user(
                request,
                f'{skipped} model(s) were not activated because they are not '
                f'approved. Approve them first.',
                level='WARNING',
            )

    @admin.action(description='❌ Reject selected models')
    def reject_models(self, request, queryset):
        from apps.accounts.audit import record as record_admin_action
        from apps.accounts.models import AdminAuditEvent

        rejected = list(queryset)
        count = queryset.update(status='rejected')
        for model in rejected:
            record_admin_action(AdminAuditEvent.Action.MODEL_REJECTED,
                                actor=request.user, target=model,
                                target_label=model.slug)
        self.message_user(request, f'{count} model(s) rejected.')


@admin.register(HealthAlert)
class HealthAlertAdmin(PhiAccessLoggedAdmin, admin.ModelAdmin):
    list_display  = ('title', 'patient', 'severity', 'is_read', 'created_at')
    list_filter   = ('severity', 'is_read')
    search_fields = ('title', 'patient__username')
    actions       = ['mark_read']

    @admin.action(description='Mark selected alerts as read')
    def mark_read(self, request, queryset):
        queryset.update(is_read=True)


@admin.register(ModelPrediction)
class ModelPredictionAdmin(PhiAccessLoggedAdmin, admin.ModelAdmin):
    list_display = ('model', 'patient', 'risk_score', 'created_at')
    list_filter  = ('model__category',)
    readonly_fields = ('input_data', 'result')
