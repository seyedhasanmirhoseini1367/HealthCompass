import uuid
from django.db import models
from django.conf import settings


class MedicalRecord(models.Model):
    class RecordType(models.TextChoices):
        LAB_RESULT   = 'lab_result',   'Lab Result'
        PRESCRIPTION = 'prescription', 'Prescription'
        DIAGNOSIS    = 'diagnosis',    'Diagnosis'
        VACCINATION  = 'vaccination',  'Vaccination'
        IMAGING      = 'imaging',      'Imaging Report'
        WEARABLE     = 'wearable',     'Wearable Data'
        DISCHARGE    = 'discharge',    'Discharge Summary'
        OTHER        = 'other',        'Other'

    class Source(models.TextChoices):
        KANTA_XML     = 'kanta_xml',     'Kanta XML Import'
        WEARABLE_CSV  = 'wearable_csv',  'Wearable Import'
        MANUAL_UPLOAD = 'manual_upload', 'Manual Upload'

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                    related_name='medical_records')
    record_type = models.CharField(max_length=20, choices=RecordType.choices, default=RecordType.OTHER)
    source      = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL_UPLOAD)
    title       = models.CharField(max_length=255)
    file        = models.FileField(upload_to='medical_records/%Y/%m/', blank=True, null=True)
    raw_text    = models.TextField(blank=True, help_text='Extracted text from file')
    parsed_data = models.JSONField(default=dict, blank=True, help_text='Structured parsed data')
    notes       = models.TextField(blank=True)
    record_date = models.DateField(null=True, blank=True, help_text='Date the record was created/measured')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    is_flagged  = models.BooleanField(default=False, help_text='Flagged by AI as having abnormal values')

    # When this record's RAG index was last built successfully.
    #
    # Indexing is dispatched to an in-process ThreadPoolExecutor with an
    # unbounded in-memory queue. A 200-record Kanta import queues 200 jobs; if
    # the container is redeployed the queue evaporates and those records are
    # simply never chunked. `retry_failed_embeddings` cannot help — it recovers
    # chunks whose embedding is NULL, and a record that never reached
    # DocumentProcessor has no chunk row to find.
    #
    # The failure the patient sees is the same one CB-2 was about: the record is
    # in their list, and the assistant says it has no such record. NULL here
    # makes those records findable so a sweep can reindex them.
    indexed_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='Set when RAG indexing last SUCCEEDED, and only then. NULL '
                  'means this record is not searchable by the assistant.')

    # ── Indexing state ───────────────────────────────────────────────────────
    #
    # indexed_at alone could not answer "is this record searchable?".
    #
    # `index_record()` returns the number of chunks it created, and the caller
    # stamped indexed_at on any positive count — but chunks are created before
    # they are embedded, and embedding can be refused (the patient has not
    # consented to external processing) or fail (the provider is down). Both
    # produced chunks, so both produced a timestamp saying "indexed" for a
    # record with no vectors at all, which is a record the assistant cannot
    # find. The stamp said the opposite of the truth in exactly the cases that
    # matter.
    #
    # There was also no claim. Two workers could index the same record
    # concurrently and both stamp it, and a third could see a fresh timestamp
    # while indexing was still running.
    #
    # So the state is explicit, and every transition is a compare-and-set
    # against the current value — see claim_for_indexing() below.

    class IndexStatus(models.TextChoices):
        PENDING  = 'pending',  'Waiting to be indexed'
        INDEXING = 'indexing', 'Being indexed now'
        INDEXED  = 'indexed',  'Searchable'
        FAILED   = 'failed',   'Indexing failed'
        BLOCKED  = 'blocked',  'Not permitted to index'

    #: States a worker may claim from.
    #:
    #: BLOCKED is claimable although it is not in the state diagram: it means
    #: the patient had not consented to external processing at the time, and
    #: consent can be granted later. Leaving those records unclaimable would
    #: make the block permanent, so granting consent would silently never make
    #: old records searchable.
    CLAIMABLE_INDEX_STATES = (IndexStatus.PENDING, IndexStatus.FAILED,
                              IndexStatus.BLOCKED)

    index_status = models.CharField(
        max_length=16, choices=IndexStatus.choices,
        default=IndexStatus.PENDING, db_index=True,
        help_text='Where this record is in the indexing lifecycle. Only '
                  '"indexed" means the assistant can find it.')
    index_started_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the current or last indexing attempt was claimed. Used '
                  'to find work abandoned by a worker that died mid-run.')
    index_error = models.CharField(
        max_length=300, blank=True, default='',
        help_text='Why the last attempt did not succeed. Error TYPE and a short '
                  'reason only — never provider output, which can quote the '
                  'document it was given.')
    index_attempts = models.PositiveIntegerField(
        default=0,
        help_text='How many times indexing has been claimed for this record.')

    #: Identity of the current claim, not just the fact that one exists.
    #:
    #: Guarding transitions on `index_status == INDEXING` alone is not enough,
    #: because two different runs occupy that same value. A worker that stalled,
    #: had its record re-claimed by someone else, and then finally returned
    #: would find the status it expected and happily overwrite the newer run's
    #: result. Matching the token as well means a transition applies only to the
    #: attempt that actually started it.
    index_claim = models.UUIDField(
        null=True, blank=True,
        help_text='Token identifying the current indexing attempt. Only the '
                  'holder may record its outcome.')

    # Identity of the ingested artifact, for idempotent upload.
    #
    # Re-uploading the same document used to create a second MedicalRecord and a
    # second full set of ParsedLabValue rows. conflict_service then reported the
    # result to the patient as a `duplicate` clinical finding — the system
    # describing its own ingestion defect as a data conflict — and every
    # trajectory calculation counted the reading twice.
    #
    # Scope is deliberately the ARTIFACT, not the facts. Two blood draws that
    # happen to carry identical values on different dates are two real events and
    # must both survive; only the same bytes arriving twice is a duplicate.
    #
    # Blank for rows created before this field existed and for any path that
    # cannot compute a stable fingerprint. The uniqueness constraint below is
    # partial for exactly that reason, so historical rows are never invalidated.
    content_hash = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        help_text='SHA-256 of the ingested content. Empty when unknown '
                  '(pre-existing rows); such rows are exempt from de-duplication.')

    class Meta:
        ordering = ['-record_date', '-uploaded_at']
        indexes = [
            models.Index(fields=['patient', '-record_date']),
        ]
        constraints = [
            # The database-level guarantee. The application checks first for a
            # graceful response, but two concurrent identical uploads would both
            # pass that check; this is what actually prevents the second row.
            models.UniqueConstraint(
                fields=['patient', 'content_hash'],
                condition=~models.Q(content_hash=''),
                name='unique_record_content_per_patient',
            ),
        ]

    def __str__(self):
        return f'{self.title} ({self.get_record_type_display()}) - {self.patient.username}'

    # ── Indexing transitions ─────────────────────────────────────────────────
    #
    # Every one of these is a single UPDATE whose WHERE clause names the state
    # it expects to find. The database decides who wins, so two workers racing
    # for the same record cannot both proceed, and a late worker cannot
    # overwrite the result of the one that actually finished.
    #
    # They return bool rather than raising: losing a race is a normal outcome
    # for a background job, not an error, and the caller's correct response is
    # to stop rather than to handle an exception.

    def claim_for_indexing(self) -> bool:
        """
        Take ownership of indexing this record. True only for the winner.

        PENDING | FAILED | BLOCKED → INDEXING, atomically. A record already
        INDEXING belongs to somebody else; one already INDEXED needs no work
        until something marks it stale.
        """
        import uuid as _uuid

        from django.db.models import F
        from django.utils import timezone as _tz

        token = _uuid.uuid4()
        claimed = type(self).objects.filter(
            pk=self.pk, index_status__in=self.CLAIMABLE_INDEX_STATES,
        ).update(
            index_status=self.IndexStatus.INDEXING,
            index_started_at=_tz.now(),
            index_attempts=F('index_attempts') + 1,
            index_error='',
            index_claim=token,
        )
        if claimed:
            self.refresh_from_db(
                fields=['index_status', 'index_started_at', 'index_attempts',
                        'index_error', 'index_claim'])
        return bool(claimed)

    def mark_indexed(self) -> bool:
        """
        INDEXING → INDEXED. The only place indexed_at is ever set.

        Guarded on INDEXING so a worker that lost the race, or one whose claim
        was reclaimed after it stalled, cannot declare success over whatever
        the current holder is doing.
        """
        from django.utils import timezone as _tz

        return bool(type(self).objects.filter(
            pk=self.pk, index_status=self.IndexStatus.INDEXING,
            index_claim=self.index_claim,
        ).update(index_status=self.IndexStatus.INDEXED,
                 indexed_at=_tz.now(), index_error='', index_claim=None))

    def mark_index_failed(self, reason: str) -> bool:
        """
        INDEXING → FAILED. indexed_at is deliberately left alone.

        A record that was searchable yesterday and failed to re-index today is
        still searchable from the older index, and clearing the timestamp would
        claim otherwise.
        """
        return bool(type(self).objects.filter(
            pk=self.pk, index_status=self.IndexStatus.INDEXING,
            index_claim=self.index_claim,
        ).update(index_status=self.IndexStatus.FAILED,
                 index_error=(reason or '')[:300], index_claim=None))

    def mark_index_blocked(self, reason: str) -> bool:
        """
        INDEXING | PENDING → BLOCKED. Refused, not broken.

        Distinct from FAILED because the responses differ: a failure should be
        retried, while a block will keep being refused until the patient
        changes their mind, and retrying it just spends quota to be told no.
        """
        from django.db.models import Q

        # From PENDING no claim is needed — that is the path taken before any
        # attempt starts. From INDEXING the token is required, for the same
        # reason the other transitions require it.
        return bool(type(self).objects.filter(
            Q(index_status=self.IndexStatus.PENDING)
            | Q(index_status=self.IndexStatus.INDEXING,
                index_claim=self.index_claim),
            pk=self.pk,
        ).update(index_status=self.IndexStatus.BLOCKED,
                 index_error=(reason or '')[:300], index_claim=None))

    def mark_index_stale(self) -> bool:
        """
        Anything except INDEXING → PENDING, because the content changed.

        Not applied to a record being indexed right now: that run is already
        working from the new content or will be superseded by the next save,
        and stealing its state mid-flight would let it finish and mark a
        version that no longer exists as current.
        """
        return bool(type(self).objects.exclude(
            index_status=self.IndexStatus.INDEXING,
        ).filter(pk=self.pk).update(index_status=self.IndexStatus.PENDING))

    @property
    def is_searchable(self) -> bool:
        """
        Whether the assistant can actually find this record.

        The question `indexed_at` was being asked and could not answer.
        """
        return self.index_status == self.IndexStatus.INDEXED


class ParsedLabValue(models.Model):
    """Individual values extracted from lab results."""
    record          = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name='lab_values')
    # Denormalised owner. Isolation used to depend on every caller remembering
    # to join `record__patient`; all of them did, but nothing enforced it, and a
    # single forgotten filter would mix one patient's analytes into another's
    # results. MedicalDocument and MedicalChunk already denormalise the patient,
    # so this also makes the pattern consistent across the models that hold PHI.
    #
    # Derived, never independently set: save() takes it from the parent record,
    # so the two cannot drift apart.
    patient         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                        related_name='lab_values', null=True, blank=True)
    parameter_name  = models.CharField(max_length=200)
    value           = models.CharField(max_length=100)
    unit            = models.CharField(max_length=50, blank=True)
    # Canonical value normalized to a consistent unit (e.g. mg/dL) for safe
    # cross-record comparison. Finnish labs report in SI (µmol/L, mmol/L);
    # without normalization trajectory thresholds would be wildly wrong.
    canonical_value = models.FloatField(null=True, blank=True)
    original_unit   = models.CharField(max_length=50, blank=True)
    # False when the analyte is known but the incoming unit was not recognised.
    # Such rows are excluded from trajectory and threshold comparisons.
    unit_known      = models.BooleanField(default=True)
    reference_range = models.CharField(max_length=100, blank=True)
    is_abnormal     = models.BooleanField(default=False)
    is_critical     = models.BooleanField(default=False)
    measured_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Without an ordering, row order is whatever the engine returns, and it
        # differs between the SQLite used in development and the Postgres used
        # in production. Anything reading "the values on this record" — the
        # doctor's record page, the export, the serializers — was relying on
        # that. Chronological with an explicit tiebreak makes it the same
        # everywhere.
        #
        # nulls_last is stated explicitly because the default differs by engine:
        # SQLite sorts NULLs first ascending, Postgres sorts them last. Undated
        # values belong after dated ones, not silently at the top.
        ordering = [models.F('measured_at').asc(nulls_last=True), 'id']
        indexes = [
            models.Index(fields=['patient', 'parameter_name']),
        ]

    def save(self, *args, **kwargs):
        # The parent record is the single source of truth for ownership.
        if self.record_id and self.patient_id != self.record.patient_id:
            self.patient_id = self.record.patient_id
        super().save(*args, **kwargs)

    def effective(self):
        """
        The reading that currently stands: the newest correction, or this row.

        Every consumer that asks "what is this value" must come through here.
        Reading the row's own fields answers "what was extracted", which is a
        different question and only correct when nothing has superseded it.

        Returns an object exposing the same value/unit/canonical_value/
        unit_known/is_abnormal/is_critical attributes either way, so callers do
        not branch.
        """
        correction = self.corrections.first()      # Meta.ordering: newest first
        return correction if correction is not None else self

    @property
    def is_corrected(self) -> bool:
        return self.corrections.exists()

    def __str__(self):
        flag = ' [!]' if self.is_critical else (' [abnormal]' if self.is_abnormal else '')
        return f'{self.parameter_name}: {self.value} {self.unit}{flag}'


class LabValueCorrection(models.Model):
    """
    A corrected reading for a lab value, appended rather than written over it.

    Values come from LLM extraction of uploaded documents, so they can be wrong:
    a misread digit, a unit the parser did not recognise. A wrong unit can raise
    a false critical alert, so corrections have to be possible. Until now the
    only way was to edit the ParsedLabValue row in the Django admin, which
    destroyed what the document actually said.

    That matters beyond tidiness. The original extraction is evidence — of what
    the source document contained and of what the patient was told at the time.
    An alert that fired on 5.2 cannot be explained by a row that now reads 52,
    and "the system said X" becomes unanswerable.

    So the original row is never mutated. A correction supersedes it, and both
    remain readable.

    Chain shape, deliberately flat
    ------------------------------
    Every correction points at the ORIGINAL ParsedLabValue, never at the
    correction before it. Correcting a correction appends another row against
    the same original. This makes a cycle structurally impossible rather than
    something to detect and reject, and "the effective value" is simply the most
    recent row — resolved by (created_at, id), so two corrections in the same
    tick still have one deterministic answer.

    Nothing here decides clinical questions. A correction records that a human
    with the authority to do so asserted a different value, and why.
    """
    # Auto-increment, deliberately NOT a UUID like the document-level models.
    # `Meta.ordering` uses the id to break a created_at tie, and that is only a
    # chronological tiebreak if ids increase. UUID4 is random, so two
    # corrections written in the same tick resolved to whichever one happened to
    # sort higher — the effective clinical value decided by chance.
    original      = models.ForeignKey('ParsedLabValue', on_delete=models.CASCADE,
                      related_name='corrections',
                      help_text='The extracted value this supersedes. Never modified.')

    # The corrected reading, in the same shape as ParsedLabValue so the two are
    # interchangeable to every consumer.
    value           = models.CharField(max_length=100)
    unit            = models.CharField(max_length=50, blank=True)
    canonical_value = models.FloatField(null=True, blank=True)
    original_unit   = models.CharField(max_length=50, blank=True)
    unit_known      = models.BooleanField(default=True)
    is_abnormal     = models.BooleanField(default=False)
    is_critical     = models.BooleanField(default=False)

    # Provenance. Required — a correction with no stated reason is
    # indistinguishable from a mistake, and this row is evidence too.
    reason        = models.TextField(
                      help_text='Why the extracted value was wrong. Recorded verbatim.')
    source        = models.CharField(max_length=200, blank=True, default='',
                      help_text='What the correction was based on — e.g. "re-read of '
                                'the source PDF", "laboratory confirmation".')
    actor         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                      null=True, blank=True, related_name='lab_value_corrections')
    # Survives deletion of the account, for the same reason DoctorAccessLog
    # denormalises its actor: a correction by "someone" is not accountable.
    actor_label   = models.CharField(max_length=200, blank=True, default='')
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest first: the effective value is the head of this ordering.
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['original', '-created_at'])]

    def __str__(self):
        return f'correction of {self.original_id} → {self.value} {self.unit}'.strip()

    def save(self, *args, **kwargs):
        if not self.actor_label and self.actor_id:
            role = getattr(self.actor, 'role', '') or ''
            self.actor_label = (f'{self.actor.username} ({role})' if role
                                else self.actor.username)
        super().save(*args, **kwargs)


class WearableDataPoint(models.Model):
    """Individual data points from wearable device CSV imports."""
    class Metric(models.TextChoices):
        HEART_RATE   = 'heart_rate',   'Heart Rate (bpm)'
        STEPS        = 'steps',        'Steps'
        SLEEP        = 'sleep',        'Sleep (hours)'
        CALORIES     = 'calories',     'Calories Burned'
        BLOOD_OXYGEN = 'blood_oxygen', 'Blood Oxygen (%)'
        WEIGHT       = 'weight',       'Weight (kg)'
        TEMPERATURE  = 'temperature',  'Body Temperature'
        OTHER        = 'other',        'Other'

    record      = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name='wearable_points')
    # Denormalised owner — see the note on ParsedLabValue.patient.
    patient     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                    related_name='wearable_points', null=True, blank=True)
    metric      = models.CharField(max_length=20, choices=Metric.choices, default=Metric.OTHER)
    value       = models.FloatField()
    unit        = models.CharField(max_length=20, blank=True)
    recorded_at = models.DateTimeField()

    class Meta:
        ordering = ['recorded_at', 'id']
        indexes = [
            models.Index(fields=['patient', 'metric', 'recorded_at']),
        ]

    def save(self, *args, **kwargs):
        if self.record_id and self.patient_id != self.record.patient_id:
            self.patient_id = self.record.patient_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.get_metric_display()}: {self.value} {self.unit} @ {self.recorded_at}'


