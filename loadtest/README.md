# Load test

Measures how many concurrent users the production worker settings carry
(2 gunicorn workers × 8 threads, audit PERF-02), with chat answered by a stub
after 5 seconds so a stream holds a thread the way a real answer does.

```bash
pip install locust==2.32.4
# a throwaway database, never production
export DEBUG=True LOADTEST_STUB_LLM_DELAY=5 RATELIMIT_ENABLE=False MFA_ENFORCED=False
python manage.py migrate
LOADTEST_USERS=50 python manage.py shell < loadtest/create_users.py
gunicorn healthcompass.wsgi:application --workers 2 --worker-class gthread --threads 8 --timeout 120 &
locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 --headless \
       -u 50 -r 2 --run-time 5m --csv loadtest/results
```

Raise `-u` step by step (10, 20, 30, …) and record, for each step, the p95 of
"records list" and "dashboard" from `loadtest/results_stats.csv`. The ceiling is
the first step where p95 for those non-chat pages passes 1 s.

## Results

Not measured yet: gunicorn does not run on Windows, where this was prepared.
Expected from the arithmetic: 16 threads, each chat stream holds one for ~5 s,
so roughly 16 / (0.2 × 5 s per request cycle) ≈ 10–20 active users before
page requests queue behind streams. Fill in the measured number here.

| Users | p95 records list | p95 dashboard | Notes |
|---|---|---|---|
| | | | |
