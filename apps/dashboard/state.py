"""
What a dashboard tile is allowed to say, and what each colour means.

Colour on a health dashboard is read as a clinical judgement whether or not one
was intended. Green next to a medication reads as "you are fine"; amber reads as
"something is wrong with you". So every state here has a definition that can be
checked against the data, and none of them is decorative.

The two that matter most are the ones usually missing:

  NO_DATA      nothing has been recorded. NOT the same as nothing being wrong.
               A patient who has never uploaded anything must not see green.
  UNAVAILABLE  we could not determine it — a permission we lack, a subsystem
               that failed. Also not green, and not amber either: the honest
               answer is that the question was not answered.

URGENT is deliberately hard to reach. It requires an alert a clinician's rule
produced (HealthAlert.CRITICAL), never a pattern this application inferred from
button presses. "Three unanswered reminders" is ATTENTION, because the evidence
is silence and silence is not an emergency.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class State:
    """Semantic states a dashboard section can be in, worst-first when sorted."""

    URGENT      = 'urgent'       # a clinically defined alert exists
    ATTENTION   = 'attention'    # an observed pattern worth a look
    CHANGE      = 'change'       # something meaningful changed recently
    UPCOMING    = 'upcoming'     # scheduled, nothing to do yet
    OK          = 'ok'           # checked, and there is genuinely nothing
    NO_DATA     = 'no_data'      # nothing recorded — unknown, not fine
    UNAVAILABLE = 'unavailable'  # could not be determined

    #: Ranking for "what needs me first". Lower sorts earlier.
    ORDER = {
        URGENT: 0, ATTENTION: 1, CHANGE: 2, UPCOMING: 3,
        OK: 4, NO_DATA: 5, UNAVAILABLE: 6,
    }

    #: The states that belong in "needs your attention". OK/NO_DATA/UNAVAILABLE
    #: are states of knowledge, not calls to action, and putting them in an
    #: attention list is how a list stops meaning anything.
    ACTIONABLE = (URGENT, ATTENTION)

    @classmethod
    def rank(cls, state: str) -> int:
        return cls.ORDER.get(state, cls.ORDER[cls.UNAVAILABLE])

    @classmethod
    def worst(cls, states) -> str:
        """The most demanding state in a group, or NO_DATA for an empty group."""
        states = [s for s in states if s]
        if not states:
            return cls.NO_DATA
        return min(states, key=cls.rank)


#: Presentation per state. Kept beside the definitions so a new state cannot be
#: rendered without someone deciding what it means.
#:
#: NO_DATA and UNAVAILABLE are deliberately grey rather than green: they are the
#: absence of an answer, and colouring them reassuringly would be the dashboard
#: telling the patient something it does not know.
STYLE = {
    State.URGENT:      {'tone': 'urgent',  'dot': '🔴', 'label': 'Needs attention now'},
    State.ATTENTION:   {'tone': 'warn',    'dot': '🟠', 'label': 'Needs attention'},
    State.CHANGE:      {'tone': 'info',    'dot': '🔵', 'label': 'Something changed'},
    State.UPCOMING:    {'tone': 'info',    'dot': '🔵', 'label': 'Coming up'},
    State.OK:          {'tone': 'ok',      'dot': '🟢', 'label': 'Nothing to do'},
    State.NO_DATA:     {'tone': 'muted',   'dot': '⚪', 'label': 'Nothing recorded yet'},
    State.UNAVAILABLE: {'tone': 'muted',   'dot': '⚪', 'label': 'Not available'},
}


@dataclass
class Item:
    """
    One thing the dashboard says, with the state that justifies saying it.

    `detail` describes what was OBSERVED. It must not draw a conclusion the data
    cannot support — "not confirmed for 3 days" rather than "not taking their
    medication". The first is a fact about this application's records; the
    second is a claim about a person.
    """
    title:  str
    state:  str = State.OK
    detail: str = ''
    href:   str = ''
    #: Optional call to action, e.g. ('Confirm now', '/care/').
    action: tuple | None = None

    @property
    def style(self) -> dict:
        return STYLE.get(self.state, STYLE[State.UNAVAILABLE])

    @property
    def is_actionable(self) -> bool:
        return self.state in State.ACTIONABLE

    @property
    def rank(self) -> int:
        return State.rank(self.state)


@dataclass
class Section:
    """A dashboard block: a headline state plus the items behind it."""
    key:   str
    title: str
    state: str = State.NO_DATA
    items: list = field(default_factory=list)
    href:  str = ''
    #: One line summarising the section without opening it.
    summary: str = ''

    @property
    def style(self) -> dict:
        return STYLE.get(self.state, STYLE[State.UNAVAILABLE])

    def settle(self) -> 'Section':
        """Take the section's state from the worst thing in it."""
        if self.items:
            self.state = State.worst(i.state for i in self.items)
        return self
