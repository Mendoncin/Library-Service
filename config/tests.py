import os
import subprocess
import sys
import textwrap
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework.test import APIClient


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


class OpenApiDocumentationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Schema generation must not access the database or external integrations.
        with patch('socket.socket.connect', side_effect=AssertionError('Unexpected connection')):
            response = APIClient().get(reverse('schema'), {'format': 'json'})
        if response.status_code != 200:
            raise AssertionError(f'Schema endpoint returned {response.status_code}')
        cls.schema = response.json()

    def response_schema(self, operation, status='200'):
        schema = operation['responses'][status]['content']['application/json']['schema']
        return self.resolve_schema(schema)

    def resolve_schema(self, schema):
        while '$ref' in schema:
            parts = schema['$ref'].removeprefix('#/').split('/')
            schema = self.schema
            for part in parts:
                schema = schema[part]
        return schema

    def test_documentation_routes_and_anonymous_access(self):
        self.assertEqual(reverse('schema'), '/api/schema/')
        self.assertEqual(reverse('swagger-ui'), '/api/docs/')
        with patch('socket.socket.connect', side_effect=AssertionError('Unexpected connection')):
            self.assertEqual(self.client.get(reverse('schema')).status_code, 200)
            response = self.client.get(reverse('swagger-ui'))
        self.assertContains(response, 'SwaggerUIBundle')
        self.assertContains(response, '/api/schema/')

    def test_custom_paths_and_methods_exist(self):
        for path, method in (
            ('/borrowings/', 'get'),
            ('/borrowings/{id}/return/', 'post'),
            ('/payments/success/', 'get'),
            ('/payments/cancel/', 'get'),
        ):
            with self.subTest(path=path, method=method):
                self.assertIn(method, self.schema['paths'][path])

    def test_borrowing_list_parameters_match_runtime_contract(self):
        operation = self.schema['paths']['/borrowings/']['get']
        parameters = {p['name']: p for p in operation['parameters']}
        for name, expected_type in [('is_active', 'boolean'), ('user_id', 'integer')]:
            with self.subTest(parameter=name):
                self.assertEqual(parameters[name]['in'], 'query')
                self.assertEqual(parameters[name]['schema']['type'], expected_type)
                self.assertFalse(parameters[name].get('required', False))
        self.assertIn('case-insensitively', parameters['is_active']['description'])
        self.assertIn('400', parameters['is_active']['description'])
        self.assertIn('Staff only', parameters['user_id']['description'])
        self.assertIn('Ignored for regular users', parameters['user_id']['description'])

    def test_return_has_no_body_and_uses_borrowing_response(self):
        operation = self.schema['paths']['/borrowings/{id}/return/']['post']
        self.assertNotIn('requestBody', operation)
        self.assertTrue({'200', '401', '404', '409'} <= operation['responses'].keys())
        detail = self.schema['paths']['/borrowings/{id}/']['get']
        self.assertEqual(self.response_schema(operation), self.response_schema(detail))
        self.assertTrue(self.response_schema(operation)['properties']['actual_return_date']['readOnly'])
        self.assertEqual(set(self.response_schema(operation, '409')['properties']), {'detail'})
        self.assertFalse(any(p['in'] == 'query' for p in operation.get('parameters', [])))

    def test_success_requires_session_id_and_limits_public_response(self):
        operation = self.schema['paths']['/payments/success/']['get']
        parameter = next(p for p in operation['parameters'] if p['name'] == 'session_id')
        self.assertEqual(parameter['in'], 'query')
        self.assertEqual(parameter['schema']['type'], 'string')
        self.assertTrue(parameter['required'])
        self.assertNotIn('requestBody', operation)
        self.assertTrue({'200', '400', '404'} <= operation['responses'].keys())
        properties = self.response_schema(operation)['properties']
        self.assertEqual(set(properties), {'detail', 'status'})
        self.assertEqual(set(self.resolve_schema(properties['status'])['enum']), {'PAID', 'PENDING'})
        self.assertEqual(set(self.response_schema(operation, '400')['properties']), {'session_id'})
        self.assertEqual(set(self.response_schema(operation, '404')['properties']), {'detail'})

    def test_cancel_has_no_input_and_only_detail_response(self):
        operation = self.schema['paths']['/payments/cancel/']['get']
        self.assertNotIn('requestBody', operation)
        self.assertFalse(operation.get('parameters'))
        self.assertEqual(set(self.response_schema(operation)['properties']), {'detail'})

    def test_security_scheme_uses_custom_authorize_header_and_bearer_prefix(self):
        schemes = self.schema['components']['securitySchemes'].values()
        matching = [s for s in schemes if s.get('in') == 'header' and s.get('name') == 'Authorize']
        self.assertTrue(matching)
        for scheme in matching:
            self.assertEqual(scheme['type'], 'apiKey')
            self.assertIn('Bearer', scheme.get('description', ''))
        self.assertFalse(any(s.get('name') == 'Authorization' for s in schemes))
        self.assertEqual(settings.SIMPLE_JWT['AUTH_HEADER_NAME'], 'HTTP_AUTHORIZE')

    def test_protected_operations_require_the_documented_jwt_scheme(self):
        scheme_names = {
            name for name, scheme in self.schema['components']['securitySchemes'].items()
            if scheme.get('in') == 'header' and scheme.get('name') == 'Authorize'
        }
        for path, method in (
            ('/borrowings/', 'get'), ('/borrowings/', 'post'),
            ('/borrowings/{id}/', 'get'), ('/borrowings/{id}/return/', 'post'),
            ('/payments/', 'get'), ('/payments/{id}/', 'get'),
            ('/users/me/', 'get'), ('/books/', 'post'), ('/books/{id}/', 'delete'),
        ):
            with self.subTest(path=path, method=method):
                operation = self.schema['paths'][path][method]
                security = operation.get('security', self.schema.get('security', []))
                self.assertTrue(security)
                self.assertNotIn({}, security)
                self.assertTrue(all(set(requirement) & scheme_names for requirement in security))

    def test_public_operations_do_not_require_jwt(self):
        for path in ('/payments/success/', '/payments/cancel/', '/books/', '/books/{id}/'):
            with self.subTest(path=path):
                operation = self.schema['paths'][path]['get']
                security = operation.get('security', self.schema.get('security', []))
                self.assertTrue(not security or {} in security)
