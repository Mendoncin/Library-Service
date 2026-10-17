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

    def test_fresh_import_loads_django_settings_without_network(self):
        # A fresh interpreter guards imports before Django loads config.
        script = textwrap.dedent("""
            from unittest.mock import patch

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
