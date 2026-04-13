# ai_insights/inference/tabular_passthrough.py
"""
Generic tabular handler — accepts CSV or Parquet, passes straight to sklearn/joblib model.

handler_slug: "tabular_passthrough"

handler_config (optional):
{
    "expected_n_features": 12,          # if set, column count is validated
    "target_columns": ["col1", "col2"], # if set, only these columns are used
    "drop_columns":   ["id", "date"],   # columns to remove before prediction
    "label_map": {"0": "Low Risk", "1": "High Risk"}
}
"""

from .registry import register
from .base import InferenceHandler, InferenceError


@register("tabular_passthrough")
class TabularPassthroughHandler(InferenceHandler):

    accepted_extensions = ['csv', 'parquet', 'tsv', 'txt']

    def load_and_preprocess(self, file, filename: str):
        if filename.endswith('.parquet'):
            df = self.read_parquet(file)
        else:
            df = self.read_csv(file)

        summary = {
            'format':   filename.rsplit('.', 1)[-1].upper(),
            'rows':     len(df),
            'columns':  list(df.columns),
        }

        # Apply column filters from config
        target = self.cfg.get('target_columns')
        if target:
            missing = set(target) - set(df.columns)
            if missing:
                raise InferenceError(
                    f'Missing required columns: {", ".join(sorted(missing))}. '
                    f'Your file has: {", ".join(df.columns[:10])}{"..." if len(df.columns) > 10 else ""}'
                )
            df = df[target]

        drop = self.cfg.get('drop_columns', [])
        if drop:
            df = df.drop(columns=[c for c in drop if c in df.columns])

        # Keep only the first row if multiple rows
        return df.iloc[[0]], summary
