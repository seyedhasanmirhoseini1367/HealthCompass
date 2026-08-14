"""
REGRESSION — F2: a model could reach patient-facing ACTIVE without approval.

The lifecycle in this repository is `pending → approved → active`, plus
`rejected` (models.py Status). There is no draft, submitted, deactivated or
retired state, so this file tests the states that exist rather than a lifecycle
the code does not implement.

Three ways in, all live before the fix:

  * `activate_models` ran `queryset.update(status='active')` over the whole
    selection with no approval check, while its sibling `approve_models`
    correctly filtered `status='pending'`. Approval was skippable by design.
  * The changelist's inline status editor and the change form let any status be
    typed directly.
  * The seizure integration created its AIModel row with `status=ACTIVE` on the
    first analysis any authenticated user ran — an ordinary user action
    publishing a never-reviewed model to a catalog that filters on `active`.

Activation is the transition that makes a model patient-facing, so it is the one
edge that has to be earned. Every other transition the four states allow is left
alone: this is one gate, not a state-machine framework.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.ai_insights.models import AIModel

User = get_user_model()
S = AIModel.Status


class _Fixture(TestCase):

    def setUp(self):
        self.scientist = User.objects.create_user(
            'gate_ds', email='gate_ds@test.invalid', password='pw', role='data_scientist')

    def _model(self, status=S.PENDING, name='Gate'):
        return AIModel.objects.create(
            data_scientist=self.scientist, name=name, description='d', status=status)


class DirectSaveTests(_Fixture):
    """The path every other path funnels through."""

    def test_a_pending_model_cannot_be_activated(self):
        """ACCEPTANCE — F2."""
        model = self._model(S.PENDING)
        model.status = S.ACTIVE
        with self.assertRaises(ValidationError):
            model.save()

        model.refresh_from_db()
        self.assertEqual(model.status, S.PENDING)

    def test_a_rejected_model_cannot_be_activated(self):
        model = self._model(S.REJECTED)
        model.status = S.ACTIVE
        with self.assertRaises(ValidationError):
            model.save()

    def test_a_model_cannot_be_created_active(self):
        """ACCEPTANCE — F2. This is how the seizure integration published one."""
        with self.assertRaises(ValidationError):
            AIModel.objects.create(
                data_scientist=self.scientist, name='Born active',
                description='d', status=S.ACTIVE)

        self.assertFalse(AIModel.objects.filter(name='Born active').exists())

    def test_an_approved_model_can_be_activated(self):
        """The gate must not block the legitimate path."""
        model = self._model(S.APPROVED)
        model.status = S.ACTIVE
        model.save()

        model.refresh_from_db()
        self.assertEqual(model.status, S.ACTIVE)

    def test_an_active_model_can_still_be_saved(self):
        """Editing an already-active model is not a re-activation."""
        model = self._model(S.APPROVED)
        model.status = S.ACTIVE
        model.save()

        model.description = 'updated description'
        model.save()

        model.refresh_from_db()
        self.assertEqual(model.description, 'updated description')

    def test_update_fields_does_not_slip_past_the_gate(self):
        model = self._model(S.PENDING)
        model.status = S.ACTIVE
        with self.assertRaises(ValidationError):
            model.save(update_fields=['status'])

    def test_a_reloaded_object_is_judged_on_its_stored_status(self):
        """The gate reads the status the row had, not the one in memory."""
        model = self._model(S.PENDING)
        fresh = AIModel.objects.get(pk=model.pk)
        fresh.status = S.ACTIVE
        with self.assertRaises(ValidationError):
            fresh.save()

    def test_the_other_transitions_are_untouched(self):
        model = self._model(S.PENDING)
        for target in (S.APPROVED, S.REJECTED, S.PENDING):
            with self.subTest(target=target):
                model.status = target
                model.save()
                model.refresh_from_db()
                self.assertEqual(model.status, target)

    def test_clean_reports_it_as_a_field_error(self):
        """So the admin form shows a message instead of an exception page."""
        model = self._model(S.PENDING)
        model.status = S.ACTIVE
        with self.assertRaises(ValidationError) as caught:
            model.clean()
        self.assertIn('status', caught.exception.message_dict)


class AdminActionTests(_Fixture):
    """queryset.update() bypasses save(), so the action filters for itself."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            'gate_admin', email='gate_admin@test.invalid', password='pw-admin-1')
        self.client.force_login(self.admin)

    def _run_action(self, action, models):
        return self.client.post(reverse('admin:ai_insights_aimodel_changelist'), {
            'action': action,
            '_selected_action': [str(m.pk) for m in models],
        }, follow=True)

    def test_the_activate_action_skips_unapproved_models(self):
        """ACCEPTANCE — F2. This activated the whole selection."""
        pending = self._model(S.PENDING, name='P')
        rejected = self._model(S.REJECTED, name='R')

        self._run_action('activate_models', [pending, rejected])

        for model in (pending, rejected):
            model.refresh_from_db()
            self.assertNotEqual(model.status, S.ACTIVE)

    def test_the_activate_action_activates_approved_models(self):
        approved = self._model(S.APPROVED, name='A')
        self._run_action('activate_models', [approved])

        approved.refresh_from_db()
        self.assertEqual(approved.status, S.ACTIVE)

    def test_a_mixed_selection_activates_only_the_approved_one(self):
        approved = self._model(S.APPROVED, name='A2')
        pending = self._model(S.PENDING, name='P2')

        self._run_action('activate_models', [approved, pending])

        approved.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(approved.status, S.ACTIVE)
        self.assertEqual(pending.status, S.PENDING)

    def test_the_skipped_models_are_reported_not_silently_ignored(self):
        pending = self._model(S.PENDING, name='P3')
        response = self._run_action('activate_models', [pending])
        messages = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('not approved', messages)

    def test_the_approve_action_still_works(self):
        pending = self._model(S.PENDING, name='P4')
        self._run_action('approve_models', [pending])

        pending.refresh_from_db()
        self.assertEqual(pending.status, S.APPROVED)

    def test_approve_then_activate_is_the_working_path(self):
        model = self._model(S.PENDING, name='Full')
        self._run_action('approve_models', [model])
        self._run_action('activate_models', [model])

        model.refresh_from_db()
        self.assertEqual(model.status, S.ACTIVE)


class NoOtherBulkUpdateTests(TestCase):
    """
    `queryset.update()` cannot be seen by `Model.save()`, so each occurrence is
    an independent bypass that must carry its own filter. There is exactly one,
    and it does.
    """

    def test_bulk_activation_happens_in_exactly_one_place(self):
        sites = self._bulk_activation_sites()
        self.assertEqual(len(sites), 1,
                         f'expected one bulk activation site, found: {sites}')

    def test_that_one_place_filters_for_approved_first(self):
        (path, _), = self._bulk_activation_sites()
        source = path.read_text(encoding='utf-8')
        self.assertIn("filter(status='approved')", source,
                      'the only bulk activation path does not check approval')

    @staticmethod
    def _bulk_activation_sites():
        import pathlib
        import re

        sites = []
        for path in pathlib.Path('apps').rglob('*.py'):
            if 'test' in path.name or 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for match in re.finditer(r"\.update\(\s*\n?\s*status\s*=\s*'active'", text):
                sites.append((path, text[:match.start()].count('\n') + 1))
        return sites


class ProvisioningTests(TestCase):
    """
    The seizure integration creates its model row on first use. Any
    authenticated user can trigger that, so it must not create an active model.
    """

    def test_the_seizure_model_is_provisioned_pending(self):
        import re
        import pathlib

        for rel in ('apps/ai_insights/views/seizure.py', 'apps/api/views/predictions.py'):
            with self.subTest(path=rel):
                text = pathlib.Path(rel).read_text(encoding='utf-8')
                # Comments stripped BEFORE slicing: the explanation of why this
                # is PENDING would otherwise fill the window and the assertion
                # would read the comment instead of the code.
                code = '\n'.join(re.sub(r'#.*$', '', line) for line in text.splitlines())
                block = code[code.index("slug='eeg-seizure-detection'"):][:600]
                self.assertNotIn('Status.ACTIVE', block)
                self.assertIn('Status.PENDING', block)

    def test_provisioning_the_model_would_now_be_refused_if_active(self):
        """The model guard is what makes the provisioning change enforceable."""
        scientist = User.objects.create_user(
            'gate_prov', email='gate_prov@test.invalid', password='pw')
        with self.assertRaises(ValidationError):
            AIModel.objects.get_or_create(
                slug='eeg-seizure-detection',
                defaults={'name': 'EEG', 'description': 'd',
                          'status': S.ACTIVE, 'data_scientist': scientist})


class CatalogVisibilityTests(_Fixture):
    """Only active models are patient-facing, which is why the gate matters."""

    def test_a_pending_model_is_not_in_the_catalog(self):
        self._model(S.PENDING, name='Hidden')
        self.assertEqual(AIModel.objects.filter(status=S.ACTIVE).count(), 0)

    def test_an_approved_model_is_not_yet_in_the_catalog(self):
        self._model(S.APPROVED, name='Waiting')
        self.assertEqual(AIModel.objects.filter(status=S.ACTIVE).count(), 0)
