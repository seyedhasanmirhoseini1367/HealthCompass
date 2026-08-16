# Care monitoring — as built

> Status: first implementation, 2026-08-15. Describes what is in the repository.
> Companion: [ARCHITECTURE.md](ARCHITECTURE.md).

The product assumption this is built against: **an older person will not open the
app every day and type in how they are.** Monitoring has to emerge from things
that happen anyway.

---

## 1. Three sources, never merged

| Source | Where it lives | Trust |
|---|---|---|
| **Passive** — device measurements, imported clinical data | `medical_records.WearableDataPoint`, `ParsedLabValue` | a machine produced a number |
| **Interaction-derived** — a reminder answered, or not | `care.TaskOccurrence` | someone pressed something, or nobody did |
| **Human-reported** — symptoms, wellbeing, voice | `care.PatientReport` | a person said something |

**There is deliberately no shared observations table.** A single table with a
`source` column is how a measured blood pressure, "I felt dizzy", and an
unanswered reminder become interchangeable rows of "a clinical fact". Three
models with three shapes means no query can flatten them by accident, and
`test_there_is_no_shared_table_the_three_sources_collapse_into` asserts that none
of them grows a common concrete base.

The unifying layer, when one is needed, is a query that carries provenance —
never a table that has already dropped it.

### Human-reported detail

`PatientReport` keeps:

- the words **verbatim** (`text`) — a cleaned-up version is an interpretation;
- **who was speaking** (`reported_by_role`) — "my mother seemed confused" is not
  "I feel confused", and merging them attributes a caregiver's impression to the
  patient;
- **how it arrived** (`input_method`), and for voice the raw `transcript` plus
  `transcript_confidence`, which is **NULL when unknown** rather than assumed
  high;
- **when it happened** (`occurred_at`) separately from when it was told to us.

---

## 2. Silence is not an answer

This is the rule the whole feature rests on.

```
PENDING       due, still inside the grace window
UNCONFIRMED   grace elapsed, nobody answered   ← the system may write ONLY this
CONFIRMED     the person said they did it      ┐
SKIPPED       the person said they chose not to├ only a human can write these
MISSED        the person said they forgot      ┘
```

A person who takes their tablet and puts the phone down leaves **exactly the
same trace** as a person who forgot. So "medication not confirmed" must never
become "medication not taken" — telling a worried son the second thing on that
evidence is a false statement about his mother's health.

Enforced, not documented:

- `TaskOccurrence.resolve()` raises `ValueError` for anything outside
  `HUMAN_STATES`, so no caller — including a crafted POST — can have the system
  assert a person's behaviour;
- `mark_unconfirmed()` only ever moves `PENDING → UNCONFIRMED`, so a late sweep
  cannot overwrite an answer someone has since given. Their statement about
  their own behaviour outranks our silence, always;
- the patient's page offers three buttons built from `HUMAN_STATES`, so
  "unconfirmed" can never appear as something a person is offered.

---

## 3. Observation → signal → policy → notification

```
observed fact  ->  MonitoringSignal  ->  NotificationEvent  ->  NotificationDelivery
                          |                     |                        |
                  carries its evidence   authorization check      one channel, one
                  (FK to the actual rows)  (SharingGrant)         outcome each
```

Each arrow adds something **we** assert rather than something the patient or a
device produced, so each is a separate row that links backwards. A signal can be
asked "which three?" and answer with the three.

`MonitoringSignal` is **not a clinical finding**. `REPEATED_UNCONFIRMED` says the
app got no answer three times. It does not say the patient is unwell, at risk, or
non-adherent — those need a clinician. A test asserts every signal kind is named
for what was observed (`repeated_*`, `reported_*`) rather than for a conclusion.

Signals are **resolved, not deleted**, when the situation stops being true: a
worry that turned out fine is worth keeping, and a caregiver who was notified
deserves to see it closed rather than vanish.

---

## 4. Thresholds are product decisions, not clinical ones

Everything in `care/policy.py` is about **how much interruption a family
tolerates**. None of it is derived from evidence about adherence or outcomes, and
the file says so, because a number that looks clinical gets quoted as though a
clinician chose it.

| Setting | Default | Why that number |
|---|---|---|
| `unconfirmed_streak_for_patient` | 1 | remind the person who can actually resolve it, first and least intrusively |
| `unconfirmed_streak_for_caregiver` | 3 | one is noise, two is a busy day; a guess about tolerance, not significance |
| `resignal_cooldown_hours` | 24 | below this, a week-long situation becomes seven identical messages |
| `notify_caregiver_on_reported_symptom` | True | someone typing "I feel dizzy" into a care app is usually asking to be heard — an assumption about people, hence a setting |
| `notify_caregiver_on_reported_missed` | True | they told us; no inference needed |

Override with `CARE_POLICY = {...}` in settings. An unknown key raises rather
than being ignored, so a typo cannot leave the default quietly in place.

**Where a real clinical rule is eventually wanted** — "three missed doses of THIS
drug matters more than three of that one" — it needs a source and a named person
who stands behind it. Nothing here invents one.

---

## 5. Notification: minimum necessary

A notification is the least controlled surface in the product. It lands on a lock
screen in a waiting room, in an inbox on a shared family tablet, on a watch face
turned toward whoever is opposite.

**Entitlement is not the test.** A caregiver may be fully entitled to read a
diagnosis in the app and still have no business receiving it as a push, because
the app is behind a login and a lock screen is not.

What a caregiver notification says:

> **Check in on Aino**
> Aino has not confirmed a scheduled care task 3 times. Open HealthCompass to
> see what needs attention.

What it never contains, asserted by tests against the **delivered** text:

- the medication or task name (the string most likely to be a drug)
- the words of a reported symptom
- a diagnosis, lab value, document title, or dose
- the username (often the email local-part — more identifying than a first name)
- any wording asserting a task was *missed* rather than *not confirmed*

The delivered body is **stored as sent**. When the question later is "did we
disclose more than we should have", the answer has to come from what was
delivered, not from re-rendering under today's rules and today's sharing scope.

### Authorization

`CARE_SCOPE = 'alerts'` — the scope whose plain-language description is "tell me
if something is wrong without letting them read my file", which is exactly what a
care notification is. Requiring `records` would force an all-or-nothing
disclosure the patient did not ask for.

Asked through `accounts.authz.sharing_grant` on **every dispatch**, never cached,
so a revocation takes effect on the next event with no separate
notification-preferences record to fall out of step.

---

## 6. Alert fatigue

Three separate defences, because this is the failure that makes the whole feature
worse than not having it — a caregiver who stops reading is a caregiver who
misses the one that mattered.

1. **Signal cooldown** (`_recent_duplicate`) — the same signal is not raised
   while it is still true.
2. **Event aggregation** (`event_for_signal`) — a repeat inside the window bumps
   `occurrence_count` on the live event; nobody is messaged again.
3. **Delivery uniqueness** — a DB constraint on
   `(event, recipient, channel)`, so a retry updates rather than adds.

---

## 7. Channels

| Channel | Available here | Notes |
|---|---|---|
| `in_app` | **yes** | a `Notification` row; no provider, no credential, no cost |
| `push` | only with `FIREBASE_CREDENTIALS_JSON` | otherwise records `UNAVAILABLE` |
| `email` | only with SMTP configured | console backend declares itself unavailable — a terminal is not delivery |
| `sms` | **no** | declared, not pretended; needs a paid provider |
| `voice` | **no** | declared; **also requires recipient verification** — see §9 |

An unavailable channel produces a `NotificationDelivery` row with status
`UNAVAILABLE`. The failure mode this replaces: `send_push` returned silently
without credentials, so a misconfigured deployment looked identical to a working
one, and the first anyone learned of it was a caregiver saying "I never got
anything".

### Two delivery defects fixed while building this

- **Push bypassed the registry.** A `post_save` receiver on `Notification` called
  `send_push` for every row, so the in-app channel implicitly also pushed — with
  no delivery record — and swallowed every exception with `except Exception:
  pass`. The receiver now skips rows the pipeline created and logs failures.
- **Appointment reminders sent two pushes.** The command created a `Notification`
  (firing the receiver) *and* called `send_push` on the next line. Two buzzes for
  one appointment is the small end of alert fatigue, and it is the end that
  teaches people to ignore notifications.

---

## 8. Caregiver dashboard

Answers one question: **does my parent need my attention?**

It is not a record browser. `test_the_caregiver_page_is_not_a_record_browser`
asserts that lab values, medication names and document titles do not appear on
it, even for a caregiver who holds a grant. A caregiver entitled to those reaches
them through the sharing pages, where each scope is evaluated separately —
otherwise one grant of "tell me if something is wrong" quietly delivers the file
as well.

Inside the app, behind the login, the caregiver **does** see the patient's own
words, marked as a quotation. The distinction is the surface, not the
entitlement.

Every read is written to the patient's own access trail
(`DoctorAccessLog resource='care:watch'`), so "who has been checking on me" has a
complete answer.

---

## 9. Voice — interface, not a clinical shortcut

Not implemented. The model support that exists is deliberate: `PatientReport`
already carries `input_method=VOICE`, the raw `transcript`, and a nullable
`transcript_confidence`, and `TaskOccurrence.response_input` records that an
answer arrived by speech.

Requirements any implementation must meet:

- **"Done" is a task event**, not a clinical fact — it goes through
  `resolve(CONFIRMED, input_method=VOICE)` like any other answer.
- **Free speech stays a report.** "I felt dizzy this morning" is preserved as a
  patient-reported observation with provenance. It is never turned into a
  diagnosis or a validated measurement.
- **Recognition can be wrong.** The transcript is kept even when corrected, so a
  mis-hearing stays visible as one. Unknown confidence is NULL, never assumed
  good.
- **AI interpretation stays separate** from the original report, in its own row,
  linked back.
- **A phone call reaches whoever picks up.** `VOICE_CHANNEL` records
  `requires recipient verification before use` in its unavailability reason.
  Health information must not simply be read aloud to whoever answers.

---

## 10. Integration boundaries — investigated, not assumed

Per the brief: none of these is assumed to be legally, technically or
commercially available, and **none blocks the core platform**.

| Integration | Technically possible? | Needs | Consent | Status |
|---|---|---|---|---|
| **Kanta / Omakanta** | Partly — the repo already imports **Kanta XML the patient exports themselves** (`upload_kanta`). A live API is a different thing: it requires Kanta-approved client status, a production certificate, and a legal basis under Finnish health-data law. | approved client registration, certificates, DPIA | explicit, per patient | **XML import: built.** Live API: not attempted; requires legal work before any code. |
| **Wearables (Fitbit/Garmin/Withings/Apple/Google)** | Yes, per-vendor OAuth REST APIs. Also already supported via **CSV upload** (`upload_wearable`). | per-vendor developer account, OAuth secrets, callback URL | explicit; also a new external-processing question | **CSV: built.** OAuth sync: adapter boundary defined below, no vendor account exists. |
| **Bluetooth BP / glucose monitors** | Only from a **native mobile app** — a Django server cannot reach BLE. Belongs in the separate mobile repo, posting to the existing API. | mobile BLE work, device pairing UX | explicit | Out of scope for this repository. |
| **SMS** | Yes, via a paid provider (Twilio, Vonage, or a Finnish operator). | paid account, sender ID, per-message cost | covered by the sharing grant | Declared as `UnavailableChannel`; `is_available()` is False. |
| **Voice/telephone** | Yes, same providers. | as SMS, **plus** recipient verification | as SMS | Declared; see §9. |

### The adapter boundary

Any passive source lands as a **measurement in `medical_records`**, never as a
`PatientReport` and never as a `TaskOccurrence`. The provenance rule holds
regardless of how the data arrived: a number from a device is a measurement, and
a device that failed to sync produces **no** measurement rather than a zero.

An importer therefore needs exactly three things, and nothing in the care app
changes when one is added:

1. authenticate to the vendor and fetch,
2. map to the existing measurement model with its unit and timestamp,
3. record what could not be fetched — a gap is a gap, not a normal reading.

`CARE_NOTIFICATION_CHANNELS` and the channel registry are the equivalent seam on
the delivery side: adding SMS means making `is_available()` true and writing
`deliver()`, with no change to domain logic.

---

## 11. Running it

```bash
python manage.py run_care_cycle              # report only — nothing written, nobody contacted
python manage.py run_care_cycle --apply      # generate, sweep, evaluate, dispatch
python manage.py run_care_cycle --apply --no-dispatch   # monitor without messaging anyone
python manage.py run_care_cycle --patient <username>    # investigate one case
```

Report-first, like `reconcile_orphaned_files`. Notifications reach real people's
phones, so seeing what a run *would* send before it sends it is not a nicety. The
dry run executes the real code inside a rolled-back transaction rather than in a
separate "pretend" mode — a second code path that only runs during dry runs is a
second code path nobody tests.

---

## 12. What is deliberately not built

- **Caregiver-created tasks.** Setting up a medication schedule for someone else
  is a care decision, and this codebase keeps administrative and caregiving
  authority separate from the patient's own. The seam is left unbuilt rather than
  half-built.
- **Deriving schedules from documents.** `MedicationStatement` records what a
  document claimed; a `CareTask` is a schedule someone set up. A discharge
  summary mentioning a drug is not a claim that a dose is due at 08:00, and
  treating it as one would invent a schedule no clinician wrote. The optional
  link between them is advisory and one-directional.
- **Escalation to clinicians.** Care signals reach family, not doctors. Routing a
  monitoring signal to a clinician is a different consent question and a
  different duty of care.
