import os
import subprocess
import sys
import textwrap

from django.conf import settings
from django.test import SimpleTestCase


class CeleryConfigTests(SimpleTestCase):
    def test_package_exports_project_app(self):
        from config import celery_app
        from config.celery import app

        self.assertIs(celery_app, app)
        self.assertEqual(app.main, 'config')
        self.assertEqual(app.namespace, 'CELERY')
        self.assertEqual(app.conf.broker_url, settings.CELERY_BROKER_URL)

    def test_fresh_import_and_autodiscovery_without_network(self):
        # A fresh interpreter guards imports before Django loads config.
        script = textwrap.dedent("""
            import sys
            from unittest.mock import patch

            import django

            with patch('socket.socket.connect', side_effect=AssertionError('Unexpected connection')) as connect:
                from config import celery_app
                from config.celery import app
                from django.conf import settings
                from django.test import override_settings

                assert celery_app is app
                assert settings.SETTINGS_MODULE == 'config.settings'
                assert settings.CELERY_BROKER_URL == 'redis://broker.invalid:6379/9'
                with override_settings(CELERY_TASK_DEFAULT_QUEUE='config-smoke'):
                    assert app.conf.task_default_queue == 'config-smoke'
                    assert app.conf.broker_url == settings.CELERY_BROKER_URL
                django.setup()
                assert 'borrowings.tasks' not in sys.modules
                celery_app.loader.import_default_modules()
                task_name = 'borrowings.tasks.check_overdue_borrowings'
                assert task_name in celery_app.tasks
                entry = celery_app.conf.beat_schedule['check-overdue-borrowings-daily']
                assert entry['task'] == task_name
                assert celery_app.tasks[task_name].name == task_name
                connect.assert_not_called()
        """)
        environment = dict(os.environ, CELERY_BROKER_URL='redis://broker.invalid:6379/9')
        environment.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_daily_beat_schedule_uses_brasilia_midnight(self):
        from celery.schedules import crontab
        from config import celery_app

        self.assertEqual(settings.TIME_ZONE, 'UTC')
        self.assertEqual(settings.CELERY_TIMEZONE, 'America/Sao_Paulo')
        self.assertEqual(celery_app.conf.timezone, 'America/Sao_Paulo')
        self.assertEqual(celery_app.conf.beat_schedule, settings.CELERY_BEAT_SCHEDULE)
        self.assertEqual(set(settings.CELERY_BEAT_SCHEDULE), {'check-overdue-borrowings-daily'})
        entry = celery_app.conf.beat_schedule['check-overdue-borrowings-daily']
        self.assertEqual(entry['task'], 'borrowings.tasks.check_overdue_borrowings')
        schedule = entry['schedule']
        self.assertIsInstance(schedule, crontab)
        self.assertEqual(schedule.minute, {0})
        self.assertEqual(schedule.hour, {0})
        self.assertEqual(schedule.day_of_week, set(range(7)))
        self.assertEqual(schedule.day_of_month, set(range(1, 32)))
        self.assertEqual(schedule.month_of_year, set(range(1, 13)))
        self.assertEqual(str(schedule.tz), 'America/Sao_Paulo')
