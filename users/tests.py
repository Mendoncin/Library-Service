from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.test import TestCase

from users.models import User
from users.serializers import UserProfileSerializer, UserRegistrationSerializer


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

    def test_authenticate_with_email_uses_default_backend(self):
        user = User.objects.create_user(
            email='reader@example.com', password='test-password',
        )
        authenticated_user = authenticate(
            email='reader@example.com', password='test-password',
        )
        self.assertEqual(authenticated_user, user)
        self.assertEqual(
            authenticated_user.backend, 'django.contrib.auth.backends.ModelBackend',
        )
        self.assertIsNone(authenticate(
            email='reader@example.com', password='wrong-password',
        ))

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


class UserRegistrationSerializerTests(TestCase):
    def setUp(self):
        self.payload = {
            'email': 'Reader@EXAMPLE.COM',
            'first_name': 'Library',
            'last_name': 'Reader',
            'password': ' test-password ',
        }

    def test_valid_registration_persists_user_with_hashed_password(self):
        serializer = UserRegistrationSerializer(data=self.payload)
        self.assertEqual(set(serializer.fields), set(self.payload))
        self.assertTrue(serializer.fields['password'].write_only)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        user.refresh_from_db()
        self.assertEqual(user.email, 'Reader@example.com')
        self.assertEqual(user.first_name, self.payload['first_name'])
        self.assertEqual(user.last_name, self.payload['last_name'])
        self.assertNotEqual(user.password, self.payload['password'])
        self.assertTrue(user.check_password(self.payload['password']))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(serializer.data, {
            'email': 'Reader@example.com',
            'first_name': 'Library',
            'last_name': 'Reader',
        })

    def test_names_are_optional_and_privilege_inputs_are_ignored(self):
        serializer = UserRegistrationSerializer(data={
            'email': 'reader@example.com',
            'password': 'test-password',
            'is_staff': True,
            'is_superuser': True,
            'groups': [1],
            'user_permissions': [1],
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(set(serializer.validated_data), {'email', 'password'})
        user = serializer.save()
        user.refresh_from_db()
        self.assertEqual(user.first_name, '')
        self.assertEqual(user.last_name, '')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.exists())
        self.assertFalse(user.user_permissions.exists())

    def test_email_is_required_and_must_be_valid(self):
        cases = (
            ({}, 'required'),
            ({'email': ''}, 'blank'),
            ({'email': None}, 'null'),
            ({'email': 'invalid-email'}, 'invalid'),
        )
        for email_data, code in cases:
            with self.subTest(email_data=email_data):
                serializer = UserRegistrationSerializer(data={
                    'password': 'test-password', **email_data,
                })
                self.assertFalse(serializer.is_valid())
                self.assertEqual(set(serializer.errors), {'email'})
                self.assertEqual(serializer.errors['email'][0].code, code)

    def test_duplicate_email_is_invalid(self):
        User.objects.create_user('reader@example.com', 'test-password')
        serializer = UserRegistrationSerializer(data={
            **self.payload, 'email': 'reader@example.com',
        })
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'email'})
        self.assertEqual(serializer.errors['email'][0].code, 'unique')

    def test_normalized_duplicate_email_is_invalid(self):
        User.objects.create_user('reader@example.com', 'test-password')
        serializer = UserRegistrationSerializer(data={
            **self.payload, 'email': 'reader@EXAMPLE.COM',
        })
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'email'})
        self.assertEqual(serializer.errors['email'][0].code, 'unique')
        self.assertEqual(User.objects.count(), 1)


class UserProfileSerializerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='reader@example.com', password='test-password',
            first_name='Library', last_name='Reader',
        )

    def test_representation_exposes_only_profile_fields(self):
        serializer = UserProfileSerializer(self.user)
        self.assertEqual(serializer.data, {
            'id': self.user.pk,
            'email': 'reader@example.com',
            'first_name': 'Library',
            'last_name': 'Reader',
            'is_staff': False,
        })
        self.assertEqual(set(serializer.fields), set(serializer.data))
        self.assertNotIn('password', serializer.fields)
        self.assertTrue(serializer.fields['id'].read_only)
        self.assertTrue(serializer.fields['is_staff'].read_only)

    def test_partial_update_persists_editable_fields(self):
        payload = {
            'email': 'updated@example.com',
            'first_name': 'Updated',
            'last_name': 'Name',
        }
        serializer = UserProfileSerializer(self.user, data=payload, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.user.refresh_from_db()
        for field, value in payload.items():
            self.assertEqual(getattr(self.user, field), value)

    def test_id_and_staff_cannot_be_changed(self):
        original_id = self.user.pk
        serializer = UserProfileSerializer(
            self.user, data={'id': original_id + 1, 'is_staff': True}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {})
        serializer.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.pk, original_id)
        self.assertFalse(self.user.is_staff)

    def test_duplicate_email_update_is_invalid(self):
        User.objects.create_user('other@example.com', 'test-password')
        serializer = UserProfileSerializer(
            self.user, data={'email': 'other@example.com'}, partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'email'})
        self.assertEqual(serializer.errors['email'][0].code, 'unique')

    def test_malformed_email_update_is_invalid(self):
        serializer = UserProfileSerializer(
            self.user, data={'email': 'invalid-email'}, partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'email'})
        self.assertEqual(serializer.errors['email'][0].code, 'invalid')

    def test_normalized_duplicate_email_update_is_invalid(self):
        other = User.objects.create_user('other@example.com', 'test-password')
        serializer = UserProfileSerializer(
            other, data={'email': 'reader@EXAMPLE.COM'}, partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'email'})
        self.assertEqual(serializer.errors['email'][0].code, 'unique')
        other.refresh_from_db()
        self.assertEqual(other.email, 'other@example.com')

    def test_normalized_email_update_preserves_local_part(self):
        serializer = UserProfileSerializer(
            self.user, data={'email': 'Other@EXAMPLE.COM'}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['email'], 'Other@example.com')
        serializer.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'Other@example.com')

    def test_normalized_own_email_does_not_conflict(self):
        user = User.objects.create_user('Reader@example.com', 'test-password')
        serializer = UserProfileSerializer(
            user, data={'email': 'Reader@EXAMPLE.COM'}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['email'], 'Reader@example.com')
        serializer.save()
        user.refresh_from_db()
        self.assertEqual(user.email, 'Reader@example.com')
