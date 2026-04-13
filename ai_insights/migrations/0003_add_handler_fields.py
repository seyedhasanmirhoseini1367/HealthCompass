from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_insights', '0002_aimodel_input_type_aimodel_interpretation_guide_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodel',
            name='handler_slug',
            field=models.CharField(
                blank=True,
                max_length=80,
                help_text=(
                    'Inference handler slug (e.g. "eeg_csv", "image_classifier", '
                    '"tabular_passthrough"). Leave blank to use the generic runner.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='aimodel',
            name='handler_config',
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text=(
                    'JSON config passed to the handler (sampling_rate_hz, label_map, etc.). '
                    'See ADMIN_CONFIGS.md in ai_insights/inference/ for examples.'
                ),
            ),
        ),
    ]
