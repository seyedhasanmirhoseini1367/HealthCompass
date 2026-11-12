"""Wearable exports: CSV, JSON, Excel, Parquet and Apple Health XML."""
import io
import re
import csv
import json
import logging
from datetime import datetime
from . import ET, DefusedXmlException  # noqa: F401  defusedxml, enforced in __init__
from .extraction import _parse_date_string

logger = logging.getLogger(__name__)


# ─── Wearable (CSV / JSON / Apple Health XML) ────────────────────────────────

# Column name mappings for common wearable exports
METRIC_COLUMN_MAP = {
    # Heart rate
    'heart rate': 'heart_rate',
    'heart_rate': 'heart_rate',
    'hr': 'heart_rate',
    'pulse': 'heart_rate',
    # Steps
    'steps': 'steps',
    'step count': 'steps',
    'total steps': 'steps',
    # Sleep
    'sleep': 'sleep',
    'sleep duration': 'sleep',
    'total sleep': 'sleep',
    'hours of sleep': 'sleep',
    # Calories
    'calories': 'calories',
    'active calories': 'calories',
    'calories burned': 'calories',
    'total calories': 'calories',
    # SpO2
    'spo2': 'blood_oxygen',
    'blood oxygen': 'blood_oxygen',
    'oxygen saturation': 'blood_oxygen',
    # Weight
    'weight': 'weight',
    'body weight': 'weight',
    # Temperature
    'temperature': 'temperature',
    'skin temperature': 'temperature',
    'body temperature': 'temperature',
}

METRIC_UNITS = {
    'heart_rate': 'bpm',
    'steps': 'steps',
    'sleep': 'hours',
    'calories': 'kcal',
    'blood_oxygen': '%',
    'weight': 'kg',
    'temperature': '°C',
    'other': '',
}


class WearableParser:
    """Parse wearable device exports: auto-detects CSV, JSON, or Apple Health XML."""

    # ── Public entry point ────────────────────────────────────────────────────

    def parse(self, data_bytes: bytes, filename: str = '') -> dict:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        stripped = data_bytes.lstrip()
        if ext == 'parquet':
            return self._parse_parquet(data_bytes, filename)
        if ext == 'json' or stripped[:1] in (b'{', b'['):
            return self._parse_json(data_bytes, filename)
        if ext == 'xml' or stripped[:1] == b'<':
            return self._parse_xml(data_bytes, filename)
        return self._parse_csv(data_bytes, filename)

    # ── CSV ───────────────────────────────────────────────────────────────────

    def _parse_csv(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            text = data_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data_bytes.decode('latin-1')

        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        device = self._detect_device(filename, headers)
        data_points, errors = [], []

        for i, row in enumerate(reader):
            try:
                data_points.extend(self._parse_row(row, headers, device))
            except Exception as e:
                errors.append(f'Row {i+2}: {e}')

        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': errors[:10]}

    # ── Parquet ───────────────────────────────────────────────────────────────

    def _parse_parquet(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            import pandas as pd
        except ImportError:
            return {'device': 'unknown', 'data_points': [], 'count': 0,
                    'errors': ['pandas is required to read Parquet files.']}
        # Parquet is compressed, so a small upload can expand to an enormous
        # frame — MAX_UPLOAD_BYTES bounds the input but not the expansion.
        # pyarrow can read the row count from the footer without materialising
        # any data, so the bomb is rejected before it is decompressed.
        from django.conf import settings as _s
        max_rows = int(getattr(_s, 'MAX_PARSED_ROWS', 200_000))
        try:
            import pyarrow.parquet as _pq
            num_rows = _pq.ParquetFile(io.BytesIO(data_bytes)).metadata.num_rows
            if num_rows > max_rows:
                return {'device': 'unknown', 'data_points': [], 'count': 0,
                        'errors': [f'File contains {num_rows:,} rows, which exceeds the '
                                   f'{max_rows:,}-row import limit.']}
        except ImportError:
            pass    # pyarrow absent: fall through, pandas will do its best
        except Exception as e:
            return {'device': 'unknown', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        try:
            df = pd.read_parquet(io.BytesIO(data_bytes))
        except Exception as e:
            return {'device': 'unknown', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        if len(df) > max_rows:
            return {'device': 'unknown', 'data_points': [], 'count': 0,
                    'errors': [f'File contains {len(df):,} rows, which exceeds the '
                               f'{max_rows:,}-row import limit.']}

        device = self._detect_device(filename, list(df.columns))
        data_points, errors = [], []

        # Normalise column names for matching
        df.columns = [str(c) for c in df.columns]

        # Find a datetime column
        dt_col = None
        for candidate in ('datetime', 'date', 'timestamp', 'time', 'startDate',
                          'start_time', 'creationDate'):
            if candidate in df.columns:
                dt_col = candidate
                break
        if dt_col is None:
            for col in df.columns:
                if 'date' in col.lower() or 'time' in col.lower():
                    dt_col = col
                    break

        for _, row in df.iterrows():
            # Parse datetime
            dt = None
            if dt_col:
                try:
                    import pandas as pd2
                    raw = row[dt_col]
                    if pd2.isna(raw):
                        continue
                    dt = pd.Timestamp(raw).to_pydatetime()
                except (ValueError, TypeError, OverflowError):
                    dt = None  # unparseable timestamp; the string fallback below tries again
            if dt is None:
                continue

            for col, raw_val in row.items():
                if col == dt_col:
                    continue
                metric = self._map_metric(str(col))
                if metric is None:
                    continue
                try:
                    value = float(raw_val)
                    if pd.isna(value):
                        continue
                except (ValueError, TypeError):
                    continue
                value, unit = self._normalize_value(metric, value, col.lower())
                data_points.append({'metric': metric, 'value': value,
                                    'unit': unit, 'recorded_at': dt.isoformat()})

        return {'device': device, 'data_points': data_points,
                'count': len(data_points), 'errors': errors[:10]}

    # ── JSON ──────────────────────────────────────────────────────────────────

    def _parse_json(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            obj = json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            return {'device': 'unknown', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        device = self._detect_device(filename, [])
        data_points = []

        # Fitbit: {"activities-heart": [{dateTime, value}], ...}
        if isinstance(obj, dict):
            for key, val in obj.items():
                if not isinstance(val, list):
                    continue
                metric = self._map_metric(key)
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    dt = self._extract_datetime(item)
                    value = self._extract_numeric(item)
                    if dt and value is not None:
                        m = metric or 'other'
                        data_points.append({'metric': m, 'value': value,
                                            'unit': METRIC_UNITS.get(m, ''), 'recorded_at': dt.isoformat()})
        # Generic array: [{date, steps, heart_rate, ...}]
        elif isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                dt = self._extract_datetime(item)
                if not dt:
                    continue
                for k, v in item.items():
                    metric = self._map_metric(k)
                    if not metric:
                        continue
                    try:
                        value = float(v)
                    except (ValueError, TypeError):
                        continue
                    data_points.append({'metric': metric, 'value': value,
                                        'unit': METRIC_UNITS.get(metric, ''), 'recorded_at': dt.isoformat()})

        if device == 'unknown':
            device = 'fitbit' if isinstance(obj, dict) else 'unknown'
        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': []}

    def _extract_numeric(self, item: dict):
        """Pull the first numeric-looking value out of a Fitbit-style {dateTime, value} dict."""
        v = item.get('value')
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            # e.g. Fitbit heart rate: {"bpm": 72, "confidence": 2}
            for sub in ('bpm', 'value', 'count'):
                if sub in v:
                    try:
                        return float(v[sub])
                    except (ValueError, TypeError):
                        pass
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    # ── Apple Health XML ──────────────────────────────────────────────────────

    _AH_TYPE_MAP = {
        'HKQuantityTypeIdentifierHeartRate':              ('heart_rate',  'bpm'),
        'HKQuantityTypeIdentifierStepCount':              ('steps',       'steps'),
        'HKQuantityTypeIdentifierActiveEnergyBurned':     ('calories',    'kcal'),
        'HKQuantityTypeIdentifierDistanceWalkingRunning': ('distance',    'km'),
        'HKQuantityTypeIdentifierOxygenSaturation':       ('blood_oxygen','%'),
        'HKQuantityTypeIdentifierBodyMass':               ('weight',      'kg'),
        'HKQuantityTypeIdentifierBodyTemperature':        ('temperature', '°C'),
        'HKQuantityTypeIdentifierRespiratoryRate':        ('respiratory_rate', 'bpm'),
        'HKCategoryTypeIdentifierSleepAnalysis':          ('sleep',       'h'),
    }

    _XML_MAX_BYTES = 50 * 1024 * 1024  # 50 MB — Apple Health exports can be hundreds of MB

    def _parse_xml(self, data_bytes: bytes, filename: str = '') -> dict:
        if len(data_bytes) > self._XML_MAX_BYTES:
            return {
                'device': 'apple_health', 'data_points': [], 'count': 0,
                'errors': [
                    f'Apple Health export exceeds the '
                    f'{self._XML_MAX_BYTES // (1024 * 1024)} MB limit. '
                    'Export a shorter date range and re-upload.'
                ],
            }

        device = 'apple_health'
        data_points, errors = [], []

        # Use stdlib iterparse for streaming — defusedxml's iterparse is
        # deprecated; the 50 MB cap above provides the size-bomb protection.
        from xml.etree.ElementTree import iterparse as _iterparse
        try:
            for _event, elem in _iterparse(io.BytesIO(data_bytes), events=('end',)):
                if elem.tag != 'Record':
                    elem.clear()
                    continue
                rtype = elem.get('type', '')
                if rtype in self._AH_TYPE_MAP:
                    metric, unit = self._AH_TYPE_MAP[rtype]
                    start_raw = elem.get('startDate') or elem.get('creationDate', '')
                    value_str = elem.get('value')
                    if start_raw and value_str is not None:
                        try:
                            dt    = datetime.strptime(start_raw[:19], '%Y-%m-%d %H:%M:%S')
                            value = float(value_str)
                            data_points.append({'metric': metric, 'value': value,
                                                'unit': unit, 'recorded_at': dt.isoformat()})
                        except (ValueError, TypeError):
                            pass
                elem.clear()
        except Exception as e:
            return {'device': 'apple_health', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': errors}

    def _detect_device(self, filename: str, headers: list) -> str:
        fname = filename.lower()
        if 'fitbit' in fname:
            return 'fitbit'
        if 'garmin' in fname:
            return 'garmin'
        if 'oura' in fname:
            return 'oura'
        if 'apple' in fname or 'health' in fname:
            return 'apple_health'
        header_str = ' '.join(h.lower() for h in headers)
        if 'fitbit' in header_str:
            return 'fitbit'
        if 'garmin' in header_str:
            return 'garmin'
        return 'unknown'

    def _parse_row(self, row: dict, headers: list, device: str) -> list:
        points = []

        # Find datetime column
        dt = self._extract_datetime(row)
        if dt is None:
            return []

        for col, raw_val in row.items():
            if not raw_val or not raw_val.strip():
                continue

            metric = self._map_metric(col)
            if metric is None:
                continue

            try:
                value = float(raw_val.replace(',', '.').strip())
            except (ValueError, AttributeError):
                continue

            # Convert units if needed
            value, unit = self._normalize_value(metric, value, col.lower())

            points.append({
                'metric': metric,
                'value': value,
                'unit': unit,
                'recorded_at': dt.isoformat(),
            })

        return points

    def _extract_datetime(self, row: dict):
        date_keys = ['datetime', 'date', 'timestamp', 'time', 'start_time',
                     'Date', 'DateTime', 'Timestamp', 'Time']
        for key in date_keys:
            if key in row and row[key]:
                return self._parse_dt(row[key])
        # Try any column that looks like a date
        for key, val in row.items():
            if 'date' in key.lower() or 'time' in key.lower():
                parsed = self._parse_dt(val)
                if parsed:
                    return parsed
        return None

    def _parse_dt(self, val: str, locale_hint: str | None = None):
        """
        Parse a datetime string from wearable data.
        Uses _parse_date_string for the date component so both wearable and
        text/PDF parsers share the same locale-aware disambiguation logic.
        """
        if not val:
            return None
        val = val.strip()

        # ISO datetime formats are always unambiguous (Y-M-D order is explicit)
        for fmt in (
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d',
        ):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue

        # Non-ISO: separate the date part from an optional time component, then
        # run the unified date parser (which handles d/m vs m/d disambiguation).
        dt_m = re.match(
            r'^(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})'
            r'(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?$',
            val,
        )
        if dt_m:
            date_part = _parse_date_string(dt_m.group(1), locale_hint=locale_hint)
            if date_part:
                time_str = dt_m.group(2)
                if time_str:
                    time_fmt = '%H:%M:%S' if time_str.count(':') == 2 else '%H:%M'
                    try:
                        return datetime.strptime(f'{date_part} {time_str}',
                                                 f'%Y-%m-%d {time_fmt}')
                    except ValueError:
                        pass
                try:
                    return datetime.strptime(date_part, '%Y-%m-%d')
                except ValueError:
                    pass

        return None

    def _map_metric(self, col: str) -> str | None:
        key = col.lower().strip()
        for pattern, metric in METRIC_COLUMN_MAP.items():
            if pattern in key:
                return metric
        return None

    def _normalize_value(self, metric: str, value: float, col_lower: str):
        unit = METRIC_UNITS.get(metric, '')
        if metric == 'weight':
            # Explicit lbs column, OR value > 350 with no kg hint → almost certainly pounds
            # (350 kg would be an extreme medical outlier; 350 lbs ≈ 159 kg is plausible)
            is_lbs = 'lb' in col_lower or (value > 350 and 'kg' not in col_lower)
            if is_lbs:
                value = round(value * 0.453592, 2)
        if metric == 'temperature':
            # Explicit °F column, OR value > 50 → must be Fahrenheit (normal body temp
            # in Celsius peaks around 42–43°C; 50°C is lethal hyperthermia)
            is_fahrenheit = 'fahrenheit' in col_lower or 'f' in col_lower or value > 50
            if is_fahrenheit:
                value = round((value - 32) * 5 / 9, 2)
        if metric == 'sleep' and value > 24:
            value = round(value / 60, 2)
        return value, unit


WearableCSVParser = WearableParser  # backward-compat alias
