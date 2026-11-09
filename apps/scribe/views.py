from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from .forms import EncounterUploadForm
from .models import Encounter
from .services.errors import AUDIO_MISSING
from .services.evidence import EVIDENCE_SECTIONS
from .services.note_generation import SECTIONS, format_timestamp
from .services import pipeline
from .services.pipeline import mark_if_stale, start_pipeline

# Upload and retry share one budget: both start a CPU-heavy transcription.
PROCESS_RATE = '10/h'
PROCESS_GROUP = 'scribe-process'

SECTION_LABELS = {'subjective': 'Subjective', 'objective': 'Objective',
                  'assessment': 'Assessment', 'plan': 'Plan'}


def _own_encounter(request, pk):
    # Scoped by owner in the query itself: another user's id is a 404, not a 403,
    # so ids cannot be probed for existence.
    return get_object_or_404(Encounter, pk=pk, owner=request.user)


def _has_llm_consent(user):
    from apps.accounts.consent import has_consent
    from apps.accounts.models import ConsentPurpose
    return has_consent(user, ConsentPurpose.EXTERNAL_LLM)


@login_required
@require_GET
def encounter_list(request):
    encounters = Encounter.objects.filter(owner=request.user)
    return render(request, 'scribe/list.html', {'encounters': encounters})


@login_required
@require_http_methods(['GET', 'POST'])
@ratelimit(key='user', rate=PROCESS_RATE, group=PROCESS_GROUP, method='POST', block=False)
def encounter_upload(request):
    has_consent = _has_llm_consent(request.user)
    form = EncounterUploadForm(request.POST or None, request.FILES or None)

    if request.method == 'POST':
        if getattr(request, 'limited', False):
            messages.error(request, 'Upload limit reached (10 per hour). Try again later.')
        elif not has_consent:
            messages.error(request, 'Grant the "external AI providers" consent first.')
        elif form.is_valid():
            audio = form.cleaned_data['audio']
            encounter = Encounter.objects.create(
                owner=request.user, audio=audio, original_filename=audio.name[:255])
            start_pipeline(encounter)
            return redirect('scribe:detail', pk=encounter.pk)

    return render(request, 'scribe/upload.html', {'form': form, 'has_consent': has_consent})


def _build_sections(note, n_segments):
    sections = []
    for key in SECTIONS:
        items = note.soap.get(key, []) if note else []
        ev = (note.evidence.get(key, []) if note else []) if key in EVIDENCE_SECTIONS else []
        sections.append({
            'key': key,
            'label': SECTION_LABELS[key],
            'show_evidence': key in EVIDENCE_SECTIONS,
            'items': [{
                'text': item['text'],
                'segments': [i for i in item['source_segments'] if i < n_segments],
                'segments_csv': ','.join(str(i) for i in item['source_segments'] if i < n_segments),
                'evidence': ev[pos] if pos < len(ev) else [],
            } for pos, item in enumerate(items)],
        })
    return sections


@login_required
@require_GET
def encounter_detail(request, pk):
    encounter = mark_if_stale(_own_encounter(request, pk))
    transcript = getattr(encounter, 'transcript', None)
    note = getattr(encounter, 'note', None)
    segments = [{'index': i, 'ts': format_timestamp(s['start']), 'text': s['text']}
                for i, s in enumerate(transcript.segments)] if transcript else []
    return render(request, 'scribe/detail.html', {
        'encounter': encounter,
        'can_retry': encounter.status == Encounter.Status.FAILED and encounter.audio_available,
        'transcript': transcript,
        'segments': segments,
        'note': note,
        'sections': _build_sections(note, len(segments)) if note else [],
    })


@login_required
@require_GET
def encounter_status(request, pk):
    encounter = mark_if_stale(_own_encounter(request, pk))
    return JsonResponse({
        'status': encounter.status,
        'status_display': encounter.get_status_display(),
        'error': encounter.error_message,
        'in_progress': encounter.is_in_progress,
    })


@login_required
@require_POST
@ratelimit(key='user', rate=PROCESS_RATE, group=PROCESS_GROUP, method='POST', block=False)
def encounter_retry(request, pk):
    encounter = mark_if_stale(_own_encounter(request, pk))
    if encounter.status != Encounter.Status.FAILED:
        messages.info(request, 'Only a failed encounter can be retried.')
    elif getattr(request, 'limited', False):
        messages.error(request, 'Processing limit reached (10 per hour). Try again later.')
    elif not _has_llm_consent(request.user):
        messages.error(request, 'Grant the "external AI providers" consent first.')
    elif not encounter.audio_available:
        pipeline.fail(encounter, AUDIO_MISSING)
    else:
        pipeline.retry(encounter)
    return redirect('scribe:detail', pk=encounter.pk)


@login_required
@require_POST
def encounter_delete(request, pk):
    encounter = _own_encounter(request, pk)
    encounter.delete()  # the post_delete signal removes the audio file
    messages.success(request, 'Encounter and its recording were deleted.')
    return redirect('scribe:list')
