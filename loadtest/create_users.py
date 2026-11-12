"""Create the load-test accounts: python manage.py shell < loadtest/create_users.py"""
import os

from django.contrib.auth import get_user_model

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose

User = get_user_model()
for i in range(1, int(os.environ.get('LOADTEST_USERS', '50')) + 1):
    user, created = User.objects.get_or_create(username=f'load{i}', defaults={'email': f'load{i}@test.invalid'})
    if created:
        user.set_password(os.environ.get('LOADTEST_PASSWORD', 'Load-Test-Pass-91'))
        user.save()
        grant_consent(user, ConsentPurpose.EXTERNAL_LLM)
print('load-test users ready')
