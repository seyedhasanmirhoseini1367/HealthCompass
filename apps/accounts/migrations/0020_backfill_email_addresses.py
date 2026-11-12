"""Create an allauth EmailAddress row for every account that lacks one.

Verification state is recorded there. Local accounts never got a row, so none
could ever count as verified and linking Google was impossible for them (audit
SEC-05). Marked verified only for accounts with a linked social account, whose
provider vouched for the address; everyone else confirms by link or by a
completed password reset.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    User = apps.get_model('accounts', 'CustomUser')
    EmailAddress = apps.get_model('account', 'EmailAddress')
    SocialAccount = apps.get_model('socialaccount', 'SocialAccount')

    social_ids = set(SocialAccount.objects.values_list('user_id', flat=True))
    have = set(EmailAddress.objects.values_list('user_id', flat=True))
    taken = {e.lower() for e in EmailAddress.objects.values_list('email', flat=True)}
    rows = []
    for user in User.objects.exclude(email='').only('pk', 'email').iterator():
        email = user.email.lower()
        if user.pk in have or email in taken:
            continue
        taken.add(email)
        rows.append(EmailAddress(user_id=user.pk, email=email, primary=True,
                                 verified=user.pk in social_ids))
    EmailAddress.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0019_share_request'),
        ('account', '0009_emailaddress_unique_primary_email'),
        ('socialaccount', '0006_alter_socialaccount_extra_data'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
