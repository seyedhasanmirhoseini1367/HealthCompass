"""
Run the scribe pipeline over a local folder of consultations and score the
generated notes against reference notes with ROUGE-L and BERTScore.

    python manage.py scribe_eval --data /path/to/data --limit 5

Expected layout (see apps/scribe/README.md):

    <data>/audio/<id>.wav|mp3|m4a                    one mixed recording, or
    <data>/audio/<id>_doctor.wav + <id>_patient.wav  two channels (PriMock57 style)
    <data>/notes/<id>.txt | <id>.json                reference note

Nothing is written to the database and evidence retrieval is skipped — only the
transcript → note path is measured. Datasets are never downloaded.
"""
import csv
import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.scribe.services.note_generation import SECTIONS, generate_note_from_segments
from apps.scribe.services.transcription import transcribe_audio

AUDIO_EXTS = ('.wav', '.mp3', '.m4a')
CHANNEL_SUFFIXES = ('_doctor', '_patient')


def discover_samples(data_dir: Path):
    """Return [(sample_id, [audio paths], reference path)] sorted by id."""
    audio_dir, notes_dir = data_dir / 'audio', data_dir / 'notes'
    if not audio_dir.is_dir() or not notes_dir.is_dir():
        raise CommandError(f'{data_dir} must contain audio/ and notes/ folders.')

    groups = {}
    for p in sorted(audio_dir.iterdir()):
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        sid = p.stem
        for suffix in CHANNEL_SUFFIXES:
            if sid.endswith(suffix):
                sid = sid[:-len(suffix)]
                break
        groups.setdefault(sid, []).append(p)

    samples = []
    for sid, paths in sorted(groups.items()):
        ref = next((notes_dir / f'{sid}{ext}' for ext in ('.txt', '.json')
                    if (notes_dir / f'{sid}{ext}').is_file()), None)
        if ref:
            samples.append((sid, paths, ref))
    return samples


def load_reference(path: Path) -> str:
    if path.suffix == '.txt':
        return path.read_text(encoding='utf-8').strip()
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        return '\n'.join(str(x) for x in data).strip()
    for key in ('note', 'reference', 'text'):
        if isinstance(data.get(key), str):
            return data[key].strip()
    raise CommandError(f'{path}: JSON note needs a "note", "reference" or "text" string.')


def load_audio(paths):
    """A single file is passed through; doctor/patient channels are mixed to one track."""
    if len(paths) == 1:
        return str(paths[0])
    import numpy as np
    from faster_whisper import decode_audio
    tracks = [decode_audio(str(p)) for p in paths]   # 16 kHz mono float32
    mixed = np.zeros(max(len(t) for t in tracks), dtype=np.float32)
    for t in tracks:
        mixed[:len(t)] += t
    peak = float(np.abs(mixed).max())
    return mixed / peak if peak > 1.0 else mixed   # only rescale if the sum clips


def note_to_text(soap) -> str:
    return '\n'.join(
        f'{section.capitalize()}:\n' + '\n'.join(f'- {i["text"]}' for i in soap.get(section, []))
        for section in SECTIONS
    )


class Command(BaseCommand):
    help = 'Evaluate the scribe pipeline (ROUGE-L, BERTScore) on a local dataset.'

    def add_arguments(self, parser):
        parser.add_argument('--data', required=True, help='Dataset folder (audio/ + notes/).')
        parser.add_argument('--limit', type=int, default=None, help='Max samples to run.')
        parser.add_argument('--out', default='scribe_eval_results.csv', help='Per-sample CSV path.')
        parser.add_argument('--bertscore-model', default='distilbert-base-uncased',
                            help='Model for BERTScore (default is small; the bert-score '
                                 'library default, roberta-large, is ~1.4 GB).')
        parser.add_argument('--no-bertscore', action='store_true', help='Skip BERTScore.')

    def handle(self, *args, **opts):
        try:
            from rouge_score import rouge_scorer
        except ImportError:
            raise CommandError('Install eval dependencies: pip install -r apps/scribe/requirements-eval.txt')
        bert_score = None
        if not opts['no_bertscore']:
            try:
                import bert_score
            except ImportError:
                raise CommandError('bert-score is not installed (pip install -r '
                                   'apps/scribe/requirements-eval.txt), or pass --no-bertscore.')

        data_dir = Path(opts['data']).expanduser().resolve()
        samples = discover_samples(data_dir)
        if opts['limit']:
            samples = samples[:opts['limit']]
        if not samples:
            raise CommandError(f'No samples with both audio and a reference note in {data_dir}.')

        scorer = rouge_scorer.RougeScorer(['rougeL'], use_stemmer=True)
        rows = []
        for n, (sid, audio_paths, ref_path) in enumerate(samples, 1):
            self.stdout.write(f'[{n}/{len(samples)}] {sid} ...')
            row = {'sample_id': sid, 'status': 'ok', 'provider': '', 'n_segments': 0,
                   'transcribe_s': '', 'generate_s': '', 'rougeL_f': '', 'bertscore_f1': '',
                   'error': '', 'reference': load_reference(ref_path), 'generated': ''}
            try:
                t0 = time.perf_counter()
                result = transcribe_audio(load_audio(audio_paths))
                row['transcribe_s'] = round(time.perf_counter() - t0, 1)
                row['n_segments'] = len(result['segments'])

                t0 = time.perf_counter()
                soap, _raw, provider = generate_note_from_segments(result['segments'])
                row['generate_s'] = round(time.perf_counter() - t0, 1)
                row['provider'] = provider
                row['generated'] = note_to_text(soap)
                row['rougeL_f'] = round(
                    scorer.score(row['reference'], row['generated'])['rougeL'].fmeasure, 4)
            except Exception as exc:  # ScribeError or decode/LLM failure — keep going
                row['status'], row['error'] = 'failed', f'{type(exc).__name__}: {exc}'
                self.stderr.write(f'    failed: {row["error"]}')
            rows.append(row)

        ok = [r for r in rows if r['status'] == 'ok']
        if bert_score and ok:
            self.stdout.write(f'Computing BERTScore with {opts["bertscore_model"]} ...')
            _, _, f1 = bert_score.score([r['generated'] for r in ok], [r['reference'] for r in ok],
                                        model_type=opts['bertscore_model'], verbose=False)
            for r, f in zip(ok, f1.tolist()):
                r['bertscore_f1'] = round(f, 4)

        out = Path(opts['out'])
        with out.open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        def mean(key):
            vals = [r[key] for r in ok if r[key] != '']
            return f'{sum(vals) / len(vals):.4f}' if vals else 'n/a'

        self.stdout.write(self.style.SUCCESS(
            f'{len(ok)}/{len(rows)} succeeded | mean ROUGE-L F {mean("rougeL_f")} | '
            f'mean BERTScore F1 {mean("bertscore_f1")} | per-sample results: {out}'))
