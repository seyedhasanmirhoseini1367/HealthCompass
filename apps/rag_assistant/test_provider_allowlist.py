"""
P1.8 — a fallback chain must not choose the vendor.

Consent is granted per purpose, "sending my health data to external AI
providers", while generation falls back across Groq, Gemini, Anthropic and
OpenAI. One consent therefore authorised four organisations, and which one
actually received a patient's records was decided by whichever API key happened
to be configured and which provider answered first.

The tests below assert the property that matters: a provider outside the
allowlist is **not invoked at all**. Not "its result is discarded" — the client
must never be constructed and the payload must never be handed over, because by
then the transfer has already happened.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.accounts.egress import phi_llm_providers, phi_provider_allowed
from apps.rag_assistant.services import generation_service as gen

CHUNKS = [{'content': 'Creatinine 180 umol/L', 'document_title': 'Labs',
           'chunk_id': 'c1', 'document_id': 'd1'}]
QUERY = 'what is my creatinine?'


class AllowlistTests(SimpleTestCase):

    def test_every_provider_is_permitted_by_default(self):
        """The default changes no behaviour; it only makes the set explicit."""
        for name in ('groq', 'gemini', 'anthropic', 'openai'):
            self.assertTrue(phi_provider_allowed(name))

    @override_settings(PHI_LLM_PROVIDERS=['groq'])
    def test_a_provider_outside_the_list_is_not_permitted(self):
        self.assertTrue(phi_provider_allowed('groq'))
        for name in ('gemini', 'anthropic', 'openai'):
            self.assertFalse(phi_provider_allowed(name))

    @override_settings(PHI_LLM_PROVIDERS=['GROQ', ' Gemini '])
    def test_the_list_is_read_forgivingly(self):
        """Operators write config by hand; case and spaces must not matter."""
        self.assertTrue(phi_provider_allowed('groq'))
        self.assertTrue(phi_provider_allowed('gemini'))
        self.assertFalse(phi_provider_allowed('openai'))

    @override_settings(PHI_LLM_PROVIDERS=[])
    def test_an_empty_list_permits_nobody(self):
        """Fails closed: an empty allowlist is a deployment that sends nothing."""
        self.assertEqual(phi_llm_providers(), frozenset())
        self.assertFalse(phi_provider_allowed('groq'))


class GenerationChainTests(SimpleTestCase):
    """The non-streaming chain."""

    def _run(self):
        return gen.generate(CHUNKS, QUERY, [])

    @override_settings(PHI_LLM_PROVIDERS=['anthropic'])
    def test_a_blocked_provider_is_never_called(self):
        """ACCEPTANCE — not "result discarded". The call must not happen."""
        with patch.object(gen, '_call_groq') as groq, \
             patch.object(gen, '_call_gemini') as gemini, \
             patch.object(gen, '_call_anthropic', return_value='answer') as anthropic, \
             patch.object(gen, '_call_openai') as openai:
            text, _, provider = self._run()

        groq.assert_not_called()
        gemini.assert_not_called()
        openai.assert_not_called()
        anthropic.assert_called_once()
        self.assertEqual(provider, 'anthropic')
        self.assertEqual(text, 'answer')

    @override_settings(PHI_LLM_PROVIDERS=['groq'])
    def test_a_failing_permitted_provider_does_not_fall_through_to_a_blocked_one(self):
        """
        ACCEPTANCE — the case the allowlist exists for.

        A chain vetted only at its first choice would reach an unvetted vendor
        precisely when the permitted one is down, which is when nobody is
        watching.
        """
        with patch.object(gen, '_call_groq', return_value=None) as groq, \
             patch.object(gen, '_call_gemini') as gemini, \
             patch.object(gen, '_call_anthropic') as anthropic, \
             patch.object(gen, '_call_openai') as openai:
            _, _, provider = self._run()

        groq.assert_called_once()
        gemini.assert_not_called()
        anthropic.assert_not_called()
        openai.assert_not_called()
        self.assertEqual(provider, 'fallback')

    @override_settings(PHI_LLM_PROVIDERS=['groq'])
    def test_a_raising_permitted_provider_also_does_not_fall_through(self):
        with patch.object(gen, '_call_groq', side_effect=RuntimeError('down')), \
             patch.object(gen, '_call_gemini') as gemini:
            _, _, provider = self._run()

        gemini.assert_not_called()
        self.assertEqual(provider, 'fallback')

    @override_settings(PHI_LLM_PROVIDERS=[])
    def test_no_permitted_provider_yields_the_local_fallback(self):
        """Nothing leaves the process, and the user still gets an answer."""
        with patch.object(gen, '_call_groq') as groq, \
             patch.object(gen, '_call_gemini') as gemini, \
             patch.object(gen, '_call_anthropic') as anthropic, \
             patch.object(gen, '_call_openai') as openai:
            text, _, provider = self._run()

        for spy in (groq, gemini, anthropic, openai):
            spy.assert_not_called()
        self.assertEqual(provider, 'fallback')
        self.assertTrue(text)


class StreamingChainTests(SimpleTestCase):
    """The streaming chain, which had no provider identity at all."""

    @override_settings(PHI_LLM_PROVIDERS=['gemini'], GROQ_API_KEY='k',
                       GEMINI_API_KEY='k', ANTHROPIC_API_KEY='k',
                       OPENAI_API_KEY='k')
    def test_a_blocked_provider_is_skipped_even_with_a_key_configured(self):
        """ACCEPTANCE — a configured key was the whole selection criterion."""
        with patch.object(gen, '_stream_groq') as groq, \
             patch.object(gen, '_stream_gemini', return_value=iter(['hi'])) as gemini, \
             patch.object(gen, '_stream_anthropic') as anthropic, \
             patch.object(gen, '_stream_openai') as openai:
            tokens = list(gen.generate_streaming(CHUNKS, QUERY, []))

        groq.assert_not_called()
        anthropic.assert_not_called()
        openai.assert_not_called()
        gemini.assert_called_once()
        self.assertEqual(tokens, ['hi'])

    @override_settings(PHI_LLM_PROVIDERS=[], GROQ_API_KEY='k')
    def test_nothing_streams_to_a_forbidden_provider(self):
        with patch.object(gen, '_stream_groq') as groq:
            tokens = list(gen.generate_streaming(CHUNKS, QUERY, []))

        groq.assert_not_called()
        self.assertTrue(tokens)          # the local fallback text

    @override_settings(PHI_LLM_PROVIDERS=['openai'], GROQ_API_KEY='k',
                       OPENAI_API_KEY='k')
    def test_the_reported_provider_matches_the_one_that_would_run(self):
        """
        A provider name in QueryLog that never saw the data is worse than no
        name, because it is believed.
        """
        self.assertEqual(gen.active_stream_provider(), 'openai')

    @override_settings(PHI_LLM_PROVIDERS=[], GROQ_API_KEY='k')
    def test_the_reported_provider_is_fallback_when_none_are_permitted(self):
        self.assertEqual(gen.active_stream_provider(), 'fallback')
