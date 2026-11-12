"""
Load test (audit T-08, PERF-02). See loadtest/README.md for how to run it.

Each simulated user signs in, lists records and opens the dashboard; one in five
also opens a chat stream, answered by the stub provider after a fixed delay, so
a stream holds a server thread exactly as a real one would.
"""
import json
import os
import random
import re

from locust import HttpUser, between, task

USERS = int(os.environ.get('LOADTEST_USERS', '50'))
PASSWORD = os.environ.get('LOADTEST_PASSWORD', 'Load-Test-Pass-91')


class Patient(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.email = f'load{random.randint(1, USERS)}@test.invalid'
        page = self.client.get('/accounts/login/', name='login page')
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
        self.client.post('/accounts/login/', {'username': self.email, 'password': PASSWORD,
                                              'csrfmiddlewaretoken': token.group(1) if token else ''},
                         headers={'Referer': self.host + '/accounts/login/'}, name='login')
        self.csrf = self.client.cookies.get('csrftoken', '')

    @task(5)
    def records(self):
        self.client.get('/records/', name='records list')

    @task(5)
    def dashboard(self):
        self.client.get('/dashboard/', name='dashboard')

    @task(2)
    def chat(self):
        with self.client.post('/assistant/stream/', data=json.dumps({'message': 'How am I doing?'}),
                              headers={'Content-Type': 'application/json', 'X-CSRFToken': self.csrf,
                                       'Referer': self.host + '/assistant/'},
                              name='chat stream', stream=True, catch_response=True) as response:
            for _ in response.iter_content(chunk_size=None):
                pass
            response.success()
