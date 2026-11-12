"""
REGRESSION — NEW-13: guardrail softening covered only the first 500 characters.

The streaming path buffered 500 characters, ran `apply()` on that buffer, and
forwarded every later token untouched. Three consequences:

1. **Softening stopped at 500 characters.** A model states its conclusion at the
   END of an answer, not the beginning, so "you have chronic kidney disease"
   arriving at character 900 reached the patient verbatim — exactly the sentence
   the rule exists to soften.

2. **A disclaimer landed mid-response.** `apply()` appends disclaimers. When a
   rule fired inside the first 500 characters, the Medication Safety Note was
   emitted in the middle of the answer and then again at the end by
   `get_appended_disclaimers()` — the same disclaimer twice.

3. **The soft consult reminder appeared mid-answer.** With no rule firing,
   `apply()` still appends "Always consult your healthcare provider…", so it was
   streamed in the middle of the response.

`soften_stream_prefix()` replaces the buffer: text is released continuously with
a short tail held back so the lookahead window is always available, and nothing
is ever appended by the streaming path.

Note on scope: these tests assert the transport property — softening reaches the
whole answer and disclaimers appear once, at the end. They do not extend which
phrasings the rule detects; that is unchanged.
"""
from django.test import SimpleTestCase

from apps.rag_assistant.services.guardrail_service import GuardrailService

CONCLUSION = 'Based on this, you have chronic kidney disease and should follow up.'


def _unsoftened(result, phrase):
    """
    Occurrences of *phrase* still stated as fact.

    The softened form keeps the words ("…may suggest you have diabetes"), so a
    plain substring check proves nothing. What matters is whether any occurrence
    lacks the hedging prefix.
    """
    lowered = result.lower()
    phrase = phrase.lower()
    return lowered.count(phrase) - lowered.count(f'may suggest {phrase}')


def _stream(text, svc=None, chunk=7):
    """Feed *text* through the streaming softener in small chunks."""
    svc = svc or GuardrailService()
    pending, out = '', []
    for i in range(0, len(text), chunk):
        pending += text[i:i + chunk]
        emitted, pending = svc.soften_stream_prefix(pending)
        out.append(emitted)
    emitted, pending = svc.soften_stream_prefix(pending, final=True)
    out.append(emitted)
    assert pending == '', 'the final flush must leave nothing behind'
    return ''.join(out)


class StreamingSoftenerTests(SimpleTestCase):

    def setUp(self):
        self.svc = GuardrailService()

    def test_benign_text_streams_through_byte_identical(self):
        text = 'Your cholesterol is 4.8 mmol/L, which is within the reference range. ' * 20
        self.assertEqual(_stream(text), text)

    def test_a_diagnosis_at_the_start_is_softened(self):
        self.assertEqual(_unsoftened(_stream(CONCLUSION), 'you have chronic kidney disease'), 0)

    def test_a_diagnosis_after_the_old_500_char_buffer_is_softened(self):
        """ACCEPTANCE — NEW-13. This is the sentence that used to stream verbatim."""
        text = 'Here is a summary of your recent results. ' * 30 + CONCLUSION
        self.assertGreater(text.index(CONCLUSION), 500)

        result = _stream(text)
        self.assertEqual(_unsoftened(result, 'you have chronic kidney disease'), 0)
        self.assertIn('may suggest you have chronic kidney disease', result)

    def test_a_diagnosis_far_into_a_long_answer_is_softened(self):
        text = 'Padding sentence about your lab history. ' * 200 + CONCLUSION
        self.assertGreater(len(text), 8000)
        self.assertEqual(
            _unsoftened(_stream(text), 'you have chronic kidney disease'), 0)

    def test_a_diagnosis_in_the_very_last_tokens_is_softened(self):
        """The final flush must soften too — nothing may escape unexamined."""
        result = _stream('Summary. ' * 100 + 'you have diabetes')
        self.assertEqual(_unsoftened(result, 'you have diabetes'), 0)

    def test_the_streamed_result_matches_a_whole_text_pass(self):
        """Chunk boundaries must not change the outcome."""
        text = 'Intro. ' * 40 + CONCLUSION + ' Tail sentence. ' * 5
        one_shot = _stream(text, chunk=len(text))
        for size in (1, 3, 17, 159, 160, 161, 512):
            with self.subTest(chunk=size):
                self.assertEqual(_stream(text, chunk=size), one_shot)

    def test_a_split_across_the_trigger_phrase_still_softens(self):
        """The phrase and its condition word can arrive in different tokens."""
        svc = GuardrailService()
        pending, out = '', []
        for token in ['Based on these results, ', 'you ', 'have ', 'diabetes', ' now.']:
            pending += token
            emitted, pending = svc.soften_stream_prefix(pending)
            out.append(emitted)
        emitted, _ = svc.soften_stream_prefix(pending, final=True)
        out.append(emitted)
        self.assertEqual(_unsoftened(''.join(out), 'you have diabetes'), 0)

    def test_multiple_diagnoses_are_all_softened(self):
        text = ('You have diabetes. ' + 'filler. ' * 100 +
                'You have hypertension. ' + 'filler. ' * 100 +
                'You have anemia.')
        result = _stream(text)
        for phrase in ['you have diabetes', 'you have hypertension', 'you have anemia']:
            with self.subTest(phrase=phrase):
                self.assertEqual(_unsoftened(result, phrase), 0)
        self.assertEqual(result.lower().count('may suggest you'), 3)

    def test_benign_you_have_phrases_are_left_alone(self):
        """
        "you have three lab results" is not a diagnosis. Softening it would make
        the assistant sound evasive about plain facts.
        """
        text = 'You have three lab results on file. ' * 20
        self.assertEqual(_stream(text), text)

    def test_no_content_is_lost_or_duplicated(self):
        """Every non-softened character must survive exactly once."""
        text = 'Alpha beta gamma delta epsilon. ' * 60
        self.assertEqual(_stream(text), text)

    def test_nothing_is_appended_by_the_streaming_path(self):
        """
        ACCEPTANCE — NEW-13(2)/(3). apply() appended disclaimers mid-stream;
        the softener must only ever soften.
        """
        for text in [CONCLUSION,
                     'You should take 500mg of metformin twice daily.',
                     'This is a medical emergency, call 911.',
                     'Your results look normal.']:
            with self.subTest(text=text[:30]):
                result = _stream(text)
                for marker in ['Medication Safety Note', 'Diagnostic Note',
                               'Urgent Reminder', 'Always consult']:
                    self.assertNotIn(marker, result)

    def test_short_answers_are_released_by_the_final_flush(self):
        """An answer shorter than the lookahead must still be emitted."""
        text = 'Normal.'
        self.assertLess(len(text), GuardrailService.STREAM_LOOKAHEAD)
        self.assertEqual(_stream(text), text)

    def test_an_empty_stream_emits_nothing(self):
        svc = GuardrailService()
        emitted, pending = svc.soften_stream_prefix('', final=True)
        self.assertEqual((emitted, pending), ('', ''))

    def test_text_is_held_back_until_its_lookahead_arrives(self):
        """
        Structural: the softener must not release a trailing "you have" before
        the words that decide whether it is a diagnosis have been seen.
        """
        svc = GuardrailService()
        emitted, pending = svc.soften_stream_prefix('Result summary. you have')
        self.assertEqual(emitted, '')
        self.assertIn('you have', pending)


class DisclaimerPlacementTests(SimpleTestCase):
    """Disclaimers belong at the end of the answer, once."""

    def setUp(self):
        self.svc = GuardrailService()

    def test_a_disclaimer_is_appended_exactly_once(self):
        text = 'You should take 500mg of metformin twice daily. ' + 'Filler. ' * 100
        streamed = _stream(text, self.svc)
        appended, rules = self.svc.get_appended_disclaimers(text)
        full = streamed + appended

        self.assertIn('dosage_recommendation', rules)
        self.assertEqual(full.count('Medication Safety Note'), 1)

    def test_the_disclaimer_is_at_the_end_not_the_middle(self):
        text = 'You should take 500mg of metformin. ' + 'Detail sentence. ' * 100
        streamed = _stream(text, self.svc)
        appended, _ = self.svc.get_appended_disclaimers(text)
        full = streamed + appended
        self.assertGreater(full.index('Medication Safety Note'), len(streamed) - 1)

    def test_the_soft_reminder_does_not_appear_mid_answer(self):
        """ACCEPTANCE — NEW-13(3)."""
        text = 'Your results are stable across all three panels. ' * 40
        streamed = _stream(text, self.svc)
        self.assertNotIn('Always consult', streamed)

        appended, rules = self.svc.get_appended_disclaimers(text)
        self.assertEqual(rules, [])
        self.assertIn('consult', appended.lower())

    def test_disclaimers_still_fire_on_content_beyond_the_old_buffer(self):
        """
        get_appended_disclaimers() always saw the full text, so this was already
        correct — pinned so the streaming rewrite cannot regress it.
        """
        text = 'Filler. ' * 200 + 'This is a medical emergency, call 911.'
        _, rules = self.svc.get_appended_disclaimers(text)
        self.assertIn('emergency_indicator', rules)
