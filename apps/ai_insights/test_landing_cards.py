"""The Analytics landing page only offers Population Insights to roles that can open it."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()
POP = reverse('ai_insights:population')


class LandingCardTests(TestCase):

    def _page(self):
        return self.client.get(reverse('ai_insights:list')).content.decode()

    def test_anonymous_does_not_see_population(self):
        self.assertNotIn(POP, self._page())

    def test_patient_does_not_see_population(self):
        u = User.objects.create_user('lc_pat', email='lc@test.invalid', password='pw', role='patient')
        self.client.force_login(u)
        body = self._page()
        self.assertNotIn(POP, body)
        self.assertIn('<span class="nc-num">2</span>', body)

    def test_staff_sees_population(self):
        u = User.objects.create_user('lc_staff', email='lcs@test.invalid', password='pw', is_staff=True)
        self.client.force_login(u)
        body = self._page()
        self.assertIn(f'href="{POP}"', body)
        self.assertIn('<span class="nc-num">3</span>', body)
