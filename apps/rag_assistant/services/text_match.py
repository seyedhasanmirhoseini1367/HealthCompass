"""
Plural-aware keyword matching for query classification.

Why this exists
---------------
Matching used `\\b<alias>\\b`, which is exact. So `platelet` did not match
"platelets" — and since `platelets` is the *canonical* biomarker name, a question
like "what are my platelets?" detected no biomarker at all and never reached the
trajectory path. Confirmed on all 15 patients of the MIMIC evaluation corpus.
The same gap affected `result`/`results` and `medication`/`medications` in the
route keyword tables.

The fix is deliberately narrow. A substring match would "solve" it while
introducing false positives — `bp` inside "bpm", `a1c` inside "ha1c", `k+`
matching anywhere — so instead each alias is expanded into a small, explicit set
of inflected forms and matched with boundaries.

Boundaries use lookarounds rather than `\\b` because several aliases end in
non-word characters (`na+`, `k+`, `25(oh)d`). `\\b` is defined relative to word
characters, so `\\bna\\+\\b` never matches; `(?<![a-z0-9])na\\+(?![a-z0-9])` does.
"""
import re
from functools import lru_cache
from typing import Set

#: Aliases short enough that inflecting them invites collisions. Matched exactly.
_NO_INFLECTION_BELOW = 3


def word_forms(word: str) -> Set[str]:
    """
    The inflected forms of one word: itself plus regular English plurals.

    Handles the cases that actually occur in the keyword tables:

        platelet   -> platelets
        result     -> results
        medication -> medications
        diagnosis  -> diagnoses      (-is -> -es)
        therapy    -> therapies      (consonant + y -> -ies)
        platelets  -> platelet       (alias already plural; accept the singular)

    Deliberately does NOT attempt irregular English (foot/feet) or Latin
    (vertebra/vertebrae). Nothing in the keyword tables needs it, and guessing
    would add matches nobody reviewed.
    """
    word = (word or '').strip().lower()
    if not word:
        return set()

    forms = {word}
    if len(word) < _NO_INFLECTION_BELOW or not word[-1].isalpha():
        # Too short, or ends in punctuation such as 'na+' / '25(oh)d'.
        return forms

    if word.endswith('is'):
        forms.add(word[:-2] + 'es')          # diagnosis -> diagnoses
    elif word.endswith('y') and len(word) > 2 and word[-2] not in 'aeiou':
        forms.add(word[:-1] + 'ies')         # therapy -> therapies
    elif word.endswith(('s', 'x', 'z', 'ch', 'sh')):
        forms.add(word + 'es')               # reflex -> reflexes
    else:
        forms.add(word + 's')                # platelet -> platelets

    # Accept the singular when the alias itself is written plural.
    if word.endswith('ies') and len(word) > 4:
        forms.add(word[:-3] + 'y')
    elif word.endswith('es') and len(word) > 3:
        forms.add(word[:-2])
        forms.add(word[:-1])
    elif word.endswith('s') and len(word) > 3:
        forms.add(word[:-1])

    return forms


@lru_cache(maxsize=2048)
def _pattern(phrase: str) -> re.Pattern:
    """
    Compile a boundary-anchored pattern for a phrase.

    Only the FINAL word is inflected: "platelet count" should match
    "platelet counts", not "platelets count".
    """
    words = (phrase or '').strip().lower().split()
    if not words:
        return re.compile(r'(?!x)x')          # matches nothing

    head = [re.escape(w) for w in words[:-1]]
    tail_forms = sorted(word_forms(words[-1]), key=lambda f: (-len(f), f))
    tail = '(?:' + '|'.join(re.escape(f) for f in tail_forms) + ')'

    body = r'\s+'.join(head + [tail]) if head else tail
    return re.compile(rf'(?<![a-z0-9]){body}(?![a-z0-9])', re.IGNORECASE)


def matches(phrase: str, text: str) -> bool:
    """True when *phrase* — in any of its inflected forms — occurs in *text*."""
    if not phrase or not text:
        return False
    return bool(_pattern(phrase).search(text))
