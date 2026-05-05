web: gunicorn healthcompass.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --log-file -
release: python manage.py migrate --noinput
