from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.test import TestCase

from users.models import User


class UserTests(TestCase):
    def test_configured_model_uses_email_without_username(self):
        self.assertIs(get_user_model(), User)
        self.assertEqual(User.USERNAME_FIELD, 'email')
        self.assertEqual(User.REQUIRED_FIELDS, [])
        email = User._meta.get_field('email')
        self.assertTrue(email.unique)
        self.assertFalse(email.blank)
        self.assertFalse(email.null)
        with self.assertRaises(FieldDoesNotExist):
            User._meta.get_field('username')

    def test_create_user_normalizes_email_and_hashes_password(self):
        user = User.objects.create_user('Reader@EXAMPLE.COM', 'test-password')
        user.refresh_from_db()
        self.assertEqual(user.email, 'Reader@example.com')
        self.assertNotEqual(user.password, 'test-password')
        self.assertTrue(user.check_password('test-password'))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_email_is_required(self):
        for method in (User.objects.create_user, User.objects.create_superuser):
            for email in ('', None):
                with self.subTest(method=method.__name__, email=email):
                    with self.assertRaisesMessage(ValueError, 'The email must be set.'):
                        method(email=email, password='test-password')
            with self.subTest(method=method.__name__, email='omitted'):
                with self.assertRaises(TypeError):
                    method(password='test-password')

    def test_email_is_unique(self):
        User.objects.create_user('reader@example.com', 'test-password')
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user('reader@example.com', 'another-password')

    def test_create_superuser_sets_flags_and_hashes_password(self):
        user = User.objects.create_superuser('admin@example.com', 'test-password')
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertNotEqual(user.password, 'test-password')
        self.assertTrue(user.check_password('test-password'))

    def test_create_superuser_rejects_inconsistent_flags(self):
        for flag in ('is_staff', 'is_superuser'):
            with self.subTest(flag=flag):
                with self.assertRaisesMessage(ValueError, f'Superuser must have {flag}=True.'):
                    User.objects.create_superuser(
                        'admin@example.com', 'test-password', **{flag: False},
                    )
