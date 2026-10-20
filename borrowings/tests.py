from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

import stripe

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from rest_framework.exceptions import ValidationError as SerializerValidationError
from rest_framework.test import APITestCase

from books.models import Book
from books.serializers import BookSerializer
from borrowings.models import Borrowing
from borrowings.serializers import BorrowingCreateSerializer, BorrowingSerializer
from borrowings.tasks import check_overdue_borrowings, get_overdue_borrowings
from borrowings.urls import urlpatterns
from borrowings.views import BorrowingViewSet
from notifications.services import format_overdue_borrowing_notification, notify_borrowing_created
from notifications.telegram import TelegramNotificationError
from payments.models import Payment
from payments.serializers import PaymentSerializer
from payments.services import convert_to_stripe_amount


class BorrowingTests(TestCase):
    def setUp(self):
        self.book = Book.objects.create(
            title='Book',
            author='Author',
            cover=Book.Cover.HARD,
            inventory=1,
            daily_fee=Decimal('1.50'),
        )
        self.user = get_user_model().objects.create_user(email='reader@example.com')
        self.payload = {
            'borrow_date': date(2026, 10, 1),
            'expected_return_date': date(2026, 10, 20),
            'actual_return_date': None,
            'book': self.book,
            'user': self.user,
        }

    def test_open_borrowing_persists_dates_and_relations(self):
        borrowing = Borrowing(**self.payload)
        borrowing.full_clean()
        borrowing.save()
        borrowing = Borrowing.objects.get(pk=borrowing.pk)
        self.assertEqual(borrowing.borrow_date, self.payload['borrow_date'])
        self.assertEqual(borrowing.expected_return_date, self.payload['expected_return_date'])
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(borrowing.book, self.book)
        self.assertEqual(borrowing.user, self.user)

    def test_expected_return_equal_to_borrow_date_is_valid(self):
        borrowing = Borrowing(
            **{**self.payload, 'expected_return_date': self.payload['borrow_date']},
        )
        borrowing.full_clean()
        borrowing.save()
        borrowing.refresh_from_db()
        self.assertEqual(borrowing.expected_return_date, borrowing.borrow_date)

    def test_expected_return_before_borrow_date_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Borrowing.objects.create(
                **{**self.payload, 'expected_return_date': date(2026, 9, 30)},
            )
        self.assertFalse(Borrowing.objects.exists())

    def test_actual_return_equal_to_borrow_date_is_valid(self):
        borrowing = Borrowing(
            **{**self.payload, 'actual_return_date': self.payload['borrow_date']},
        )
        borrowing.full_clean()
        borrowing.save()
        borrowing.refresh_from_db()
        self.assertEqual(borrowing.actual_return_date, borrowing.borrow_date)

    def test_early_return_is_valid(self):
        borrowing = Borrowing(
            **{**self.payload, 'actual_return_date': date(2026, 10, 10)},
        )
        borrowing.full_clean()
        borrowing.save()
        borrowing.refresh_from_db()
        self.assertEqual(borrowing.actual_return_date, date(2026, 10, 10))

    def test_actual_return_on_or_after_expected_return_is_valid(self):
        for actual_return_date in (date(2026, 10, 20), date(2026, 10, 21)):
            with self.subTest(actual_return_date=actual_return_date):
                borrowing = Borrowing(
                    **{**self.payload, 'actual_return_date': actual_return_date},
                )
                borrowing.full_clean()
                borrowing.save()
                borrowing.refresh_from_db()
                self.assertEqual(borrowing.actual_return_date, actual_return_date)

    def test_actual_return_before_borrow_date_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Borrowing.objects.create(
                **{**self.payload, 'actual_return_date': date(2026, 9, 30)},
            )
        self.assertFalse(Borrowing.objects.exists())

    def test_full_clean_validates_date_constraints(self):
        for field, constraint in (
            ('expected_return_date', 'borrowing_expected_return_not_before_borrow'),
            ('actual_return_date', 'borrowing_actual_return_not_before_borrow'),
        ):
            with self.subTest(field=field):
                borrowing = Borrowing(**{**self.payload, field: date(2026, 9, 30)})
                with self.assertRaisesMessage(ValidationError, constraint):
                    borrowing.full_clean()

    def test_borrowing_protects_book_and_user_from_deletion(self):
        borrowing = Borrowing.objects.create(**self.payload)
        for related_object in (self.book, self.user):
            with self.subTest(model=related_object.__class__.__name__):
                with self.assertRaises(ProtectedError):
                    related_object.delete()
        self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())


class BorrowingReadTests(APITestCase):
    def setUp(self):
        self.password = 'borrowings-read-test-password'
        self.user_a = get_user_model().objects.create_user(
            email='reader-a@example.com', password=self.password,
        )
        self.user_b = get_user_model().objects.create_user(
            email='reader-b@example.com', password=self.password,
        )
        self.borrowings = []
        for index, (user, actual_return_date) in enumerate((
            (self.user_a, None),
            (self.user_a, date(2026, 10, 10)),
            (self.user_b, None),
            (self.user_b, date(2026, 10, 10)),
        )):
            book = Book.objects.create(
                title=f'Book {index}',
                author=f'Author {index}',
                cover=Book.Cover.HARD,
                inventory=index + 1,
                daily_fee=Decimal('2.50'),
            )
            self.borrowings.append(Borrowing.objects.create(
                borrow_date=date(2026, 10, 1),
                expected_return_date=date(2026, 10, 20),
                actual_return_date=actual_return_date,
                book=book,
                user=user,
            ))
        self.borrowing = self.borrowings[0]
        self.list_url = reverse('borrowing-list')
        self.detail_url = reverse('borrowing-detail', args=[self.borrowing.pk])

    def authenticate(self, user):
        response = self.client.post(reverse('token-obtain-pair'), {
            'email': user.email,
            'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        access = response.data['access']
        self.assertTrue(access)
        self.client.credentials(HTTP_AUTHORIZE=f'Bearer {access}')
        return access

    def authenticate_staff(self):
        staff = get_user_model().objects.create_user(
            email='staff@example.com', password=self.password,
            is_staff=True, is_superuser=False,
        )
        self.assertTrue(staff.is_staff)
        self.assertFalse(staff.is_superuser)
        self.authenticate(staff)
        return staff

    def test_serializer_exposes_exact_read_contract(self):
        for borrowing in self.borrowings:
            with self.subTest(borrowing=borrowing.pk):
                self.assertEqual(BorrowingSerializer(borrowing).data, {
                    'id': borrowing.pk,
                    'borrow_date': '2026-10-01',
                    'expected_return_date': '2026-10-20',
                    'actual_return_date': (
                        borrowing.actual_return_date.isoformat()
                        if borrowing.actual_return_date else None
                    ),
                    'book': BookSerializer(borrowing.book).data,
                    'user': borrowing.user_id,
                    'payments': [],
                })

    def test_serializer_fields_are_read_only(self):
        serializer = BorrowingSerializer()
        self.assertIsInstance(serializer.fields['book'], BookSerializer)
        self.assertIsInstance(serializer.fields['payments'].child, PaymentSerializer)
        for name, field in serializer.fields.items():
            with self.subTest(field=name):
                self.assertTrue(field.read_only)

    def test_anonymous_cannot_list_borrowings(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 401)

    def test_user_lists_only_own_borrowings_with_detailed_books(self):
        self.authenticate(self.user_a)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        own_borrowings = self.borrowings[:2]
        ids = [item['id'] for item in response.data]
        self.assertEqual(ids, [borrowing.pk for borrowing in own_borrowings])
        for borrowing in self.borrowings[2:]:
            self.assertNotIn(borrowing.pk, ids)
        self.assertEqual(response.data, BorrowingSerializer(own_borrowings, many=True).data)
        for item, borrowing in zip(response.data, own_borrowings):
            self.assertIsInstance(item['book'], dict)
            self.assertEqual(item['book'], BookSerializer(borrowing.book).data)
            self.assertEqual(item['user'], borrowing.user_id)

    def test_anonymous_cannot_retrieve_borrowing(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 401)

    def test_user_can_retrieve_own_borrowing(self):
        self.authenticate(self.user_a)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, BorrowingSerializer(self.borrowing).data)

    def test_user_cannot_retrieve_other_users_borrowing(self):
        self.authenticate(self.user_a)
        other = self.borrowings[2]
        response = self.client.get(reverse('borrowing-detail', args=[other.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.data), {'detail'})

    def test_staff_can_list_all_borrowings(self):
        self.authenticate_staff()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.data],
            [borrowing.pk for borrowing in self.borrowings],
        )
        self.assertEqual(response.data, BorrowingSerializer(self.borrowings, many=True).data)

    def test_staff_can_retrieve_other_users_borrowing(self):
        self.authenticate_staff()
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, BorrowingSerializer(self.borrowing).data)

    def test_missing_borrowing_returns_404(self):
        self.authenticate(self.user_a)
        url = reverse('borrowing-detail', args=[self.borrowings[-1].pk + 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_update_and_delete_are_not_allowed(self):
        self.authenticate(self.user_a)
        original_borrowings = list(Borrowing.objects.order_by('pk').values())
        original_books = list(Book.objects.order_by('pk').values())
        payload = {
            'borrow_date': '2026-10-02',
            'expected_return_date': '2026-10-21',
            'actual_return_date': '2026-10-11',
            'book': self.borrowing.book_id,
            'user': self.borrowing.user_id,
        }
        for method, url in (
            ('put', self.detail_url),
            ('patch', self.detail_url),
            ('delete', self.detail_url),
        ):
            with self.subTest(method=method):
                response = getattr(self.client, method)(url, payload, format='json')
                self.assertEqual(response.status_code, 405)
                self.assertEqual(list(Borrowing.objects.order_by('pk').values()), original_borrowings)
                self.assertEqual(list(Book.objects.order_by('pk').values()), original_books)

    def test_anonymous_missing_detail_and_unmapped_methods_require_authentication(self):
        missing_url = reverse('borrowing-detail', args=[self.borrowings[-1].pk + 1])
        self.assertEqual(self.client.get(missing_url).status_code, 401)
        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(self.detail_url)
                self.assertEqual(response.status_code, 401)

    def test_authorization_header_does_not_authenticate(self):
        access = self.authenticate(self.user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 401)

    def test_user_active_filter_preserves_visibility(self):
        self.authenticate(self.user_a)
        for value, expected in (
            ('true', [self.borrowings[0].pk]),
            ('false', [self.borrowings[1].pk]),
            ('TRUE', [self.borrowings[0].pk]),
            ('False', [self.borrowings[1].pk]),
        ):
            with self.subTest(is_active=value):
                response = self.client.get(self.list_url, {'is_active': value})
                self.assertEqual(response.status_code, 200)
                ids = [item['id'] for item in response.data]
                self.assertEqual(ids, expected)
                for borrowing in self.borrowings[2:]:
                    self.assertNotIn(borrowing.pk, ids)

    def test_staff_active_filter_includes_all_users(self):
        self.authenticate_staff()
        for value, expected in (
            ('true', [self.borrowings[0].pk, self.borrowings[2].pk]),
            ('false', [self.borrowings[1].pk, self.borrowings[3].pk]),
        ):
            with self.subTest(is_active=value):
                response = self.client.get(self.list_url, {'is_active': value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item['id'] for item in response.data], expected)

    def test_empty_active_filter_matches_unfiltered_visible_queryset(self):
        self.authenticate(self.user_a)
        for role in ('user', 'staff'):
            with self.subTest(role=role):
                if role == 'staff':
                    self.authenticate_staff()
                expected = self.borrowings[:2] if role == 'user' else self.borrowings
                response = self.client.get(self.list_url, {'is_active': ''})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item['id'] for item in response.data],
                    [borrowing.pk for borrowing in expected],
                )

    def test_invalid_active_filter_returns_field_error(self):
        self.authenticate(self.user_a)
        for value in ('yes', '1', 'banana'):
            with self.subTest(is_active=value):
                response = self.client.get(self.list_url, {'is_active': value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {
                    'is_active': ["Expected 'true' or 'false'."],
                })

    def test_detail_ignores_active_filter_but_preserves_visibility(self):
        self.authenticate(self.user_a)
        returned = self.borrowings[1]
        for value in ('true', 'banana'):
            with self.subTest(is_active=value):
                response = self.client.get(
                    reverse('borrowing-detail', args=[returned.pk]), {'is_active': value},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, BorrowingSerializer(returned).data)
                response = self.client.get(
                    reverse('borrowing-detail', args=[self.borrowings[3].pk]),
                    {'is_active': value},
                )
                self.assertEqual(response.status_code, 404)

    def test_staff_filters_borrowings_by_user_id(self):
        self.authenticate_staff()
        for user, expected, excluded in (
            (self.user_a, self.borrowings[:2], self.borrowings[2:]),
            (self.user_b, self.borrowings[2:], self.borrowings[:2]),
        ):
            for value in (str(user.pk), f'0{user.pk}'):
                with self.subTest(user_id=value):
                    response = self.client.get(self.list_url, {'user_id': value})
                    self.assertEqual(response.status_code, 200)
                    ids = [item['id'] for item in response.data]
                    self.assertEqual(ids, [borrowing.pk for borrowing in expected])
                    for borrowing in excluded:
                        self.assertNotIn(borrowing.pk, ids)

    def test_staff_nonexistent_user_id_returns_empty_list(self):
        self.authenticate_staff()
        missing_id = 2**63 - 1
        self.assertFalse(get_user_model().objects.filter(pk=missing_id).exists())
        response = self.client.get(self.list_url, {'user_id': missing_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_staff_empty_user_id_matches_unfiltered_list(self):
        self.authenticate_staff()
        response = self.client.get(self.list_url, {'user_id': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.client.get(self.list_url).data)
        self.assertEqual(
            [item['id'] for item in response.data],
            [borrowing.pk for borrowing in self.borrowings],
        )

    def test_staff_invalid_user_id_returns_field_error(self):
        self.authenticate_staff()
        for value in (
            'abc', '1.5', '0', '-1', '+5', '²', '9223372036854775808',
            ' 5', '5 ', '1_0', '9' * 5000,
        ):
            with self.subTest(user_id=value):
                response = self.client.get(self.list_url, {'user_id': value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {
                    'user_id': ['Expected a positive integer user id within the 64-bit range.'],
                })

    def test_normal_user_ignores_user_id(self):
        self.authenticate(self.user_a)
        for value in (self.user_b.pk, self.user_a.pk, 'abc', '9223372036854775808'):
            with self.subTest(user_id=value):
                response = self.client.get(self.list_url, {'user_id': value})
                self.assertEqual(response.status_code, 200)
                ids = [item['id'] for item in response.data]
                self.assertEqual(ids, [borrowing.pk for borrowing in self.borrowings[:2]])
                for borrowing in self.borrowings[2:]:
                    self.assertNotIn(borrowing.pk, ids)

    def test_user_detail_ignores_user_id_and_preserves_visibility(self):
        self.authenticate(self.user_a)
        for value in (self.user_b.pk, 'abc'):
            with self.subTest(user_id=value):
                response = self.client.get(self.detail_url, {'user_id': value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, BorrowingSerializer(self.borrowing).data)
                response = self.client.get(
                    reverse('borrowing-detail', args=[self.borrowings[2].pk]),
                    {'user_id': value},
                )
                self.assertEqual(response.status_code, 404)

    def test_staff_detail_ignores_user_id(self):
        self.authenticate_staff()
        borrowing = self.borrowings[2]
        for value in (self.user_a.pk, 'abc'):
            with self.subTest(user_id=value):
                response = self.client.get(
                    reverse('borrowing-detail', args=[borrowing.pk]), {'user_id': value},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, BorrowingSerializer(borrowing).data)

    def test_staff_combines_user_id_and_active_filters(self):
        self.authenticate_staff()
        for user, is_active, expected in (
            (self.user_a, 'true', self.borrowings[0]),
            (self.user_a, 'false', self.borrowings[1]),
            (self.user_b, 'true', self.borrowings[2]),
        ):
            with self.subTest(user_id=user.pk, is_active=is_active):
                response = self.client.get(self.list_url, {
                    'user_id': user.pk, 'is_active': is_active,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item['id'] for item in response.data], [expected.pk])

    def test_normal_user_combined_filters_preserve_visibility(self):
        self.authenticate(self.user_a)
        for user_id, is_active, expected in (
            (self.user_b.pk, 'true', self.borrowings[0]),
            (self.user_a.pk, 'false', self.borrowings[1]),
            ('abc', 'true', self.borrowings[0]),
        ):
            with self.subTest(user_id=user_id, is_active=is_active):
                response = self.client.get(self.list_url, {
                    'user_id': user_id, 'is_active': is_active,
                })
                self.assertEqual(response.status_code, 200)
                ids = [item['id'] for item in response.data]
                self.assertEqual(ids, [expected.pk])
                for borrowing in self.borrowings[2:]:
                    self.assertNotIn(borrowing.pk, ids)

    def test_staff_invalid_combined_filters_return_first_field_error(self):
        self.authenticate_staff()
        for user_id, is_active, error_key in (
            ('abc', 'true', 'user_id'),
            (self.user_a.pk, 'banana', 'is_active'),
            ('abc', 'banana', 'user_id'),
        ):
            with self.subTest(user_id=user_id, is_active=is_active):
                response = self.client.get(self.list_url, {
                    'user_id': user_id, 'is_active': is_active,
                })
                self.assertEqual(response.status_code, 400)
                self.assertEqual(set(response.data), {error_key})

    def test_routes_expose_only_list_retrieve_create_and_return(self):
        self.assertEqual(self.list_url, '/borrowings/')
        self.assertEqual(self.detail_url, f'/borrowings/{self.borrowing.pk}/')
        return_url = reverse('borrowing-return', args=[self.borrowing.pk])
        self.assertEqual(return_url, f'/borrowings/{self.borrowing.pk}/return/')
        match = resolve(return_url)
        self.assertIs(match.func.cls, BorrowingViewSet)
        self.assertEqual(match.func.actions, {'post': 'return_borrowing'})
        self.assertEqual(len(urlpatterns), 3)
        self.assertEqual({pattern.name for pattern in urlpatterns}, {
            'borrowing-list', 'borrowing-detail', 'borrowing-return',
        })
        for url, action in ((self.list_url, 'list'), (self.detail_url, 'retrieve')):
            with self.subTest(url=url):
                match = resolve(url)
                self.assertIs(match.func.cls, BorrowingViewSet)
                self.assertEqual(match.func.actions['get'], action)
                expected_actions = {'get': action}
                if action == 'list':
                    expected_actions['post'] = 'create'
                self.assertEqual(
                    {method: action for method, action in match.func.actions.items() if method != 'head'},
                    expected_actions,
                )

    def test_nested_payments_order_shape_and_visibility(self):
        # Insert IDs out of order so creation order cannot satisfy the contract.
        payment = Payment.objects.create(
            id=30, borrowing=self.borrowing, type=Payment.Type.PAYMENT,
            money_to_pay=Decimal('17.50'), session_id='cs_read',
            session_url='https://checkout.stripe.com/c/pay/cs_read',
        )
        fine = Payment.objects.create(
            id=10, borrowing=self.borrowing, type=Payment.Type.FINE,
            money_to_pay=Decimal('3.00'), status=Payment.Status.PAID,
        )
        foreign = Payment.objects.create(
            id=20, borrowing=self.borrowings[2], type=Payment.Type.PAYMENT,
            money_to_pay=Decimal('7.50'),
        )
        expected = PaymentSerializer([fine, payment], many=True).data
        self.authenticate(self.user_a)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data], [
            item.pk for item in self.borrowings[:2]
        ])
        self.assertEqual(response.data[0]['payments'], expected)
        self.assertEqual(response.data[1]['payments'], [])
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['payments'], expected)
        foreign_url = reverse('borrowing-detail', args=[self.borrowings[2].pk])
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
        self.authenticate_staff()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 4)
        self.assertEqual(response.data[0]['payments'], expected)
        self.assertEqual(response.data[2]['payments'], [PaymentSerializer(foreign).data])
        response = self.client.get(foreign_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['payments'], [PaymentSerializer(foreign).data])

    def test_nested_payments_do_not_add_queries_per_borrowing(self):
        # A separate user makes the comparison exactly one versus three borrowings.
        user = get_user_model().objects.create_user(email='query-count@example.com')
        self.client.force_authenticate(user=user)

        def add_borrowing():
            borrowing = Borrowing.objects.create(
                user=user, book=self.borrowing.book, borrow_date=date(2026, 10, 1),
                expected_return_date=date(2026, 10, 20),
            )
            Payment.objects.create(
                borrowing=borrowing, type=Payment.Type.PAYMENT,
                money_to_pay=Decimal('17.50'),
            )

        add_borrowing()
        with CaptureQueriesContext(connection) as one:
            first = self.client.get(self.list_url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data), 1)
        add_borrowing()
        add_borrowing()
        with CaptureQueriesContext(connection) as three:
            second = self.client.get(self.list_url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.data), 3)
        for item in second.data:
            self.assertEqual(len(item['payments']), 1)
        self.assertEqual(len(one), len(three))
        self.assertEqual(sum('payments_payment' in q['sql'] for q in three), 1)


class BorrowingReturnAPITests(APITestCase):
    def setUp(self):
        self.today = date(2026, 10, 15)
        date_patch = patch('borrowings.views.timezone.localdate', return_value=self.today)
        self.localdate = date_patch.start()
        self.addCleanup(date_patch.stop)
        self.password = 'borrowings-return-test-password'
        self.user = get_user_model().objects.create_user(
            email='reader@example.com', password=self.password,
        )
        self.other_user = get_user_model().objects.create_user(email='other@example.com')
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=2, daily_fee=Decimal('2.50'),
        )
        self.borrowing = Borrowing.objects.create(
            user=self.user, book=self.book, borrow_date=date(2026, 10, 1),
            expected_return_date=date(2026, 10, 20),
        )
        self.other_borrowing = Borrowing.objects.create(
            user=self.other_user, book=self.book, borrow_date=date(2026, 10, 1),
            expected_return_date=date(2026, 10, 20),
        )
        self.url = reverse('borrowing-return', args=[self.borrowing.pk])
        self.other_url = reverse('borrowing-return', args=[self.other_borrowing.pk])
        self.authenticate(self.user)

    def authenticate(self, user):
        token_url = reverse('token-obtain-pair')
        self.assertEqual(token_url, '/users/token/')
        response = self.client.post(token_url, {
            'email': user.email, 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        access = response.data['access']
        self.assertTrue(access)
        self.client.credentials(HTTP_AUTHORIZE=f'Bearer {access}')
        return access

    def assert_rejected_without_changes(self, url, expected_status, method='post'):
        borrowings_before = list(Borrowing.objects.order_by('pk').values())
        books_before = list(Book.objects.order_by('pk').values())
        response = getattr(self.client, method)(url)
        self.assertEqual(response.status_code, expected_status)
        self.borrowing.refresh_from_db()
        self.other_borrowing.refresh_from_db()
        self.assertEqual(list(Borrowing.objects.order_by('pk').values()), borrowings_before)
        self.assertEqual(list(Book.objects.order_by('pk').values()), books_before)
        return response

    def test_user_returns_own_borrowing_with_updated_read_contract(self):
        inventory_before = self.book.inventory
        self.assertIsNone(self.borrowing.actual_return_date)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.borrowing.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, self.today)
        self.assertEqual(response.data, BorrowingSerializer(self.borrowing).data)
        self.assertEqual(set(response.data), {
            'id', 'borrow_date', 'expected_return_date', 'actual_return_date', 'book', 'user', 'payments',
        })
        self.assertEqual(response.data['actual_return_date'], self.today.isoformat())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, inventory_before + 1)
        self.assertEqual(response.data['book']['inventory'], inventory_before + 1)

    def test_payload_cannot_control_return_date(self):
        response = self.client.post(self.url, {
            'actual_return_date': '2000-01-01',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.borrowing.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, self.today)

    def test_return_on_borrow_date_is_accepted(self):
        self.localdate.return_value = self.borrowing.borrow_date
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.borrowing.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, self.borrowing.borrow_date)

    def test_other_users_borrowing_is_not_visible(self):
        self.assert_rejected_without_changes(self.other_url, 404)
        self.assertIsNone(self.other_borrowing.actual_return_date)
        self.localdate.assert_not_called()

    def test_staff_non_superuser_can_return_other_users_borrowing(self):
        inventory_before = self.book.inventory
        staff = get_user_model().objects.create_user(
            email='staff@example.com', password=self.password,
            is_staff=True, is_superuser=False,
        )
        self.assertTrue(staff.is_staff)
        self.assertFalse(staff.is_superuser)
        self.authenticate(staff)
        response = self.client.post(self.other_url)
        self.assertEqual(response.status_code, 200)
        self.other_borrowing.refresh_from_db()
        self.assertEqual(self.other_borrowing.actual_return_date, self.today)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, inventory_before + 1)

    def test_anonymous_post_requires_authentication(self):
        self.client.credentials()
        self.assert_rejected_without_changes(self.url, 401)

    def test_authorization_header_does_not_authenticate_return(self):
        access = self.authenticate(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assert_rejected_without_changes(self.url, 401)

    def test_missing_borrowing_returns_404(self):
        missing_pk = self.other_borrowing.pk + 1
        self.assertFalse(Borrowing.objects.filter(pk=missing_pk).exists())
        self.assert_rejected_without_changes(
            reverse('borrowing-return', args=[missing_pk]), 404,
        )

    def test_authenticated_wrong_methods_return_405(self):
        for method in ('get', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assert_rejected_without_changes(self.url, 405, method)

    def test_anonymous_wrong_methods_require_authentication(self):
        self.client.credentials()
        for method in ('get', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assert_rejected_without_changes(self.url, 401, method)

    def test_list_filters_do_not_affect_return_or_visibility(self):
        query = f'?is_active=false&user_id={self.other_user.pk}'
        response = self.client.post(self.url + query)
        self.assertEqual(response.status_code, 200)
        self.borrowing.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, self.today)
        self.assert_rejected_without_changes(self.other_url + query, 404)

    def test_staff_return_ignores_invalid_list_filters(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = self.client.post(self.other_url + '?is_active=invalid&user_id=invalid')
        self.assertEqual(response.status_code, 200)
        self.other_borrowing.refresh_from_db()
        self.assertEqual(self.other_borrowing.actual_return_date, self.today)

    def test_existing_return_is_rejected_with_conflict(self):
        original_date = date(2026, 10, 10)
        self.borrowing.actual_return_date = original_date
        self.borrowing.save(update_fields=['actual_return_date'])
        response = self.assert_rejected_without_changes(self.url, 409)
        self.assertEqual(response.json(), {
            'detail': 'This borrowing has already been returned.',
        })
        self.assertEqual(self.borrowing.actual_return_date, original_date)

    def test_second_return_does_not_overwrite_first_return_date(self):
        inventory_before = self.book.inventory
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.borrowing.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, self.today)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, inventory_before + 1)
        self.localdate.return_value = self.today + timedelta(days=1)
        response = self.assert_rejected_without_changes(self.url, 409)
        self.assertEqual(response.json(), {
            'detail': 'This borrowing has already been returned.',
        })
        self.assertEqual(self.borrowing.actual_return_date, self.today)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, inventory_before + 1)

    def test_each_returned_borrowing_restores_one_unit(self):
        Book.objects.filter(pk=self.book.pk).update(inventory=0)
        second_borrowing = Borrowing.objects.create(
            user=self.user, book=self.book, borrow_date=self.borrowing.borrow_date,
            expected_return_date=self.borrowing.expected_return_date,
        )
        for borrowing, expected_inventory in ((self.borrowing, 1), (second_borrowing, 2)):
            with self.subTest(borrowing=borrowing.pk):
                response = self.client.post(reverse('borrowing-return', args=[borrowing.pk]))
                self.assertEqual(response.status_code, 200)
                self.book.refresh_from_db()
                borrowing.refresh_from_db()
                self.assertEqual(self.book.inventory, expected_inventory)
                self.assertEqual(borrowing.actual_return_date, self.today)

    def test_book_update_failure_rolls_back_return(self):
        inventory_before = self.book.inventory
        original_update = QuerySet.update
        updated_models = []

        def update_or_fail(queryset, **kwargs):
            updated_models.append(queryset.model)
            if queryset.model is Book:
                self.assertEqual(
                    Borrowing.objects.get(pk=self.borrowing.pk).actual_return_date,
                    self.today,
                )
                raise DatabaseError('forced book update failure')
            return original_update(queryset, **kwargs)

        with patch.object(QuerySet, 'update', autospec=True, side_effect=update_or_fail):
            with self.assertRaisesMessage(DatabaseError, 'forced book update failure'):
                self.client.post(self.url)

        self.assertEqual(updated_models, [Borrowing, Book])
        self.borrowing.refresh_from_db()
        self.book.refresh_from_db()
        self.assertIsNone(self.borrowing.actual_return_date)
        self.assertEqual(self.book.inventory, inventory_before)

    def test_conditional_update_rejects_return_completed_after_lookup(self):
        inventory_before = self.book.inventory
        concurrent_date = self.today - timedelta(days=1)
        original_get_object = BorrowingViewSet.get_object

        def get_object_then_concurrent_return(view):
            borrowing = original_get_object(view)
            Borrowing.objects.filter(pk=borrowing.pk).update(actual_return_date=concurrent_date)
            self.assertIsNone(borrowing.actual_return_date)
            return borrowing

        with patch.object(
            BorrowingViewSet, 'get_object', autospec=True,
            side_effect=get_object_then_concurrent_return,
        ):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {
            'detail': 'This borrowing has already been returned.',
        })
        self.borrowing.refresh_from_db()
        self.book.refresh_from_db()
        self.assertEqual(self.borrowing.actual_return_date, concurrent_date)
        self.assertEqual(self.book.inventory, inventory_before)

    def test_other_users_already_returned_borrowing_stays_invisible(self):
        original_date = date(2026, 10, 10)
        self.other_borrowing.actual_return_date = original_date
        self.other_borrowing.save(update_fields=['actual_return_date'])
        self.assert_rejected_without_changes(self.other_url, 404)
        self.assertEqual(self.other_borrowing.actual_return_date, original_date)
        self.localdate.assert_not_called()

    def test_return_moves_borrowing_from_active_to_inactive_list(self):
        list_url = reverse('borrowing-list')
        response = self.client.get(list_url, {'is_active': 'true'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.borrowing.pk, [item['id'] for item in response.data])
        self.assertEqual(self.client.post(self.url).status_code, 200)
        for is_active, expected_ids in (('true', []), ('false', [self.borrowing.pk])):
            with self.subTest(is_active=is_active):
                response = self.client.get(list_url, {'is_active': is_active})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item['id'] for item in response.data], expected_ids)


class BorrowingCreateSerializerTests(TestCase):
    def setUp(self):
        self.today = date(2026, 10, 13)
        date_patch = patch('borrowings.serializers.timezone.localdate', return_value=self.today)
        self.localdate = date_patch.start()
        self.addCleanup(date_patch.stop)
        self.book = Book.objects.create(
            title='Book',
            author='Author',
            cover=Book.Cover.HARD,
            inventory=2,
            daily_fee=Decimal('2.50'),
        )
        self.user = get_user_model().objects.create_user(email='reader@example.com')
        self.payload = {
            'book': self.book.pk,
            'expected_return_date': (self.today + timedelta(days=7)).isoformat(),
        }

    def test_serializer_exposes_only_create_fields(self):
        serializer = BorrowingCreateSerializer()
        self.assertEqual(set(serializer.fields), {'id', 'book', 'expected_return_date', 'payments'})
        self.assertTrue(serializer.fields['id'].read_only)
        self.assertTrue(serializer.fields['payments'].read_only)
        self.assertIsInstance(serializer.fields['payments'].child, PaymentSerializer)
        for name in ('book', 'expected_return_date'):
            with self.subTest(field=name):
                self.assertFalse(serializer.fields[name].read_only)
                self.assertTrue(serializer.fields[name].required)

    def test_valid_creation_persists_borrowing_and_decreases_inventory(self):
        serializer = BorrowingCreateSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        borrowing = serializer.save(user=self.user)
        borrowing.refresh_from_db()
        self.book.refresh_from_db()
        self.assertEqual(Borrowing.objects.count(), 1)
        self.assertEqual(borrowing.book, self.book)
        self.assertEqual(borrowing.user, self.user)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertEqual(borrowing.expected_return_date, self.today + timedelta(days=7))
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(self.book.inventory, 1)
        self.assertEqual(serializer.data, {'id': borrowing.pk, **self.payload, 'payments': []})

    def test_zero_inventory_is_rejected_during_validation(self):
        Book.objects.filter(pk=self.book.pk).update(inventory=0)
        serializer = BorrowingCreateSerializer(data=self.payload)
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors, {'book': ['This book is out of stock.']})
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 0)
        self.assertFalse(Borrowing.objects.exists())

    def test_inventory_exhausted_after_validation_is_rejected_on_save(self):
        Book.objects.filter(pk=self.book.pk).update(inventory=1)
        serializer = BorrowingCreateSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        Book.objects.filter(pk=self.book.pk).update(inventory=0)
        with self.assertRaises(SerializerValidationError) as caught:
            serializer.save(user=self.user)
        self.assertEqual(caught.exception.detail, {'book': 'This book is out of stock.'})
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 0)
        self.assertFalse(Borrowing.objects.exists())

    def test_insert_failure_rolls_back_inventory_decrement(self):
        serializer = BorrowingCreateSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(IntegrityError):
            serializer.save()
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_expected_return_before_today_is_rejected(self):
        serializer = BorrowingCreateSerializer(data={
            **self.payload,
            'expected_return_date': (self.today - timedelta(days=1)).isoformat(),
        })
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors, {
            'expected_return_date': ['Expected return date cannot be before today.'],
        })
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_expected_return_today_is_rejected_by_minimum(self):
        serializer = BorrowingCreateSerializer(data={
            **self.payload, 'expected_return_date': self.today.isoformat(),
        })
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors, {
            'non_field_errors': ['Borrowing total must be at least $0.50.'],
        })
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_missing_book_is_rejected(self):
        serializer = BorrowingCreateSerializer(data={**self.payload, 'book': self.book.pk + 1})
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'book'})
        self.assertEqual(serializer.errors['book'][0].code, 'does_not_exist')
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_payload_cannot_control_server_fields(self):
        other_user = get_user_model().objects.create_user(email='other@example.com')
        serializer = BorrowingCreateSerializer(data={
            **self.payload,
            'id': 999,
            'user': other_user.pk,
            'borrow_date': '2020-01-01',
            'actual_return_date': self.today.isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(set(serializer.validated_data), {'book', 'expected_return_date'})
        borrowing = serializer.save(user=self.user)
        borrowing.refresh_from_db()
        self.assertNotEqual(borrowing.pk, 999)
        self.assertEqual(borrowing.user, self.user)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertIsNone(borrowing.actual_return_date)

    def test_date_change_after_validation_is_rejected_before_decrement(self):
        serializer = BorrowingCreateSerializer(data={
            **self.payload, 'expected_return_date': (self.today + timedelta(days=1)).isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.localdate.return_value = self.today + timedelta(days=2)
        with self.assertRaises(SerializerValidationError) as caught:
            serializer.save(user=self.user)
        self.assertEqual(caught.exception.detail, {
            'expected_return_date': 'Expected return date cannot be before today.',
        })
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_midnight_minimum_recheck_precedes_inventory_update(self):
        serializer = BorrowingCreateSerializer(data={
            **self.payload,
            'expected_return_date': (self.today + timedelta(days=1)).isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.localdate.return_value = self.today + timedelta(days=1)
        with patch('borrowings.serializers.Book.objects.filter') as book_filter:
            with self.assertRaises(SerializerValidationError) as caught:
                serializer.save(user=self.user)
        book_filter.assert_not_called()
        self.assertEqual(caught.exception.detail, {
            'non_field_errors': ['Borrowing total must be at least $0.50.'],
        })
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_book_and_expected_return_date_are_required(self):
        serializer = BorrowingCreateSerializer(data={})
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'book', 'expected_return_date'})
        for errors in serializer.errors.values():
            self.assertEqual(errors[0].code, 'required')


@override_settings(
    TELEGRAM_BOT_TOKEN='test-token', TELEGRAM_CHAT_ID='12345', STRIPE_SECRET_KEY='',
)
class BorrowingCreateAPITests(APITestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            id='cs_test_fake', url='https://checkout.stripe.com/c/pay/cs_test_fake',
        )
        self.checkout_patch = patch(
            'borrowings.views.create_checkout_session', return_value=self.session,
        )
        self.checkout_mock = self.checkout_patch.start()
        self.addCleanup(self.checkout_patch.stop)
        notify_patch = patch('borrowings.views.notify_borrowing_created')
        self.notify_mock = notify_patch.start()
        self.addCleanup(notify_patch.stop)
        self.today = date(2026, 10, 13)
        date_patch = patch('borrowings.serializers.timezone.localdate', return_value=self.today)
        date_patch.start()
        self.addCleanup(date_patch.stop)
        self.book = Book.objects.create(
            title='Book',
            author='Author',
            cover=Book.Cover.HARD,
            inventory=2,
            daily_fee=Decimal('2.50'),
        )
        credentials = {'email': 'reader@example.com', 'password': 'borrowings-test-password'}
        self.user = get_user_model().objects.create_user(**credentials)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        token_url = reverse('token-obtain-pair')
        self.assertEqual(token_url, '/users/token/')
        response = self.client.post(token_url, credentials, format='json')
        self.assertEqual(response.status_code, 200)
        access = response.data['access']
        self.assertTrue(access)
        self.client.credentials(HTTP_AUTHORIZE=f'Bearer {access}')
        self.list_url = reverse('borrowing-list')
        self.payload = {
            'book': self.book.pk,
            'expected_return_date': (self.today + timedelta(days=7)).isoformat(),
        }

    def test_regular_user_can_create_borrowing_with_jwt(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(self.list_url, self.payload, format='json')
            self.notify_mock.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        self.notify_mock.assert_called_once()
        self.assertEqual(self.notify_mock.call_args.args[0].pk, response.data['id'])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.renderer_context['request'].user, self.user)
        self.assertEqual(Borrowing.objects.count(), 1)
        borrowing = Borrowing.objects.get(pk=response.data['id'])
        self.assertEqual(response.data, {
            'id': borrowing.pk, **self.payload,
            'payments': PaymentSerializer(borrowing.payments.all(), many=True).data,
        })
        self.assertEqual(response.data, BorrowingCreateSerializer(borrowing).data)
        self.assertEqual(borrowing.book, self.book)
        self.assertEqual(borrowing.user, self.user)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertEqual(borrowing.expected_return_date, self.today + timedelta(days=7))
        self.assertIsNone(borrowing.actual_return_date)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

    def test_payload_cannot_override_current_user_or_server_dates(self):
        other_user = get_user_model().objects.create_user(email='other@example.com')
        response = self.client.post(self.list_url, {
            **self.payload,
            'user': other_user.pk,
            'user_id': other_user.pk,
            'borrow_date': '2020-01-01',
            'actual_return_date': self.today.isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 201)
        borrowing = Borrowing.objects.get(pk=response.data['id'])
        self.assertEqual(borrowing.user, self.user)
        self.assertNotEqual(borrowing.user, other_user)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertIsNone(borrowing.actual_return_date)

    def test_staff_creates_borrowing_for_self(self):
        credentials = {'email': 'staff@example.com', 'password': 'borrowings-test-password'}
        staff = get_user_model().objects.create_user(
            **credentials, is_staff=True, is_superuser=False,
        )
        response = self.client.post(reverse('token-obtain-pair'), credentials, format='json')
        self.assertEqual(response.status_code, 200)
        access = response.data['access']
        self.client.credentials(HTTP_AUTHORIZE=f'Bearer {access}')
        response = self.client.post(self.list_url, {
            **self.payload, 'user': self.user.pk, 'user_id': self.user.pk,
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.renderer_context['request'].user, staff)
        borrowing = Borrowing.objects.get(pk=response.data['id'])
        self.assertEqual(borrowing.user, staff)
        self.assertEqual(borrowing.book, self.book)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

    def test_create_ignores_user_id_query_parameter(self):
        other = get_user_model().objects.create_user(email='other-query@example.com')
        # Staff would parse user_id on list; create must ignore it entirely.
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        for index, value in enumerate((str(other.pk), 'abc')):
            with self.subTest(user_id=value):
                response = self.client.post(
                    f'{self.list_url}?user_id={value}', self.payload, format='json',
                )
                self.assertEqual(response.status_code, 201)
                borrowing = Borrowing.objects.get(pk=response.data['id'])
                self.assertEqual(response.renderer_context['request'].user, self.user)
                self.assertEqual(borrowing.user, self.user)
                self.assertNotEqual(borrowing.user, other)
                self.book.refresh_from_db()
                self.assertEqual(self.book.inventory, 1 - index)

    def test_anonymous_cannot_create_borrowing(self):
        self.client.credentials()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(callbacks, [])
        self.notify_mock.assert_not_called()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.checkout_mock.assert_not_called()

    def test_out_of_stock_returns_400_without_creating_borrowing(self):
        Book.objects.filter(pk=self.book.pk).update(inventory=0)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(callbacks, [])
        self.notify_mock.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'book': ['This book is out of stock.']})
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 0)
        self.checkout_mock.assert_not_called()

    def test_past_expected_return_returns_400_without_decreasing_inventory(self):
        response = self.client.post(self.list_url, {
            **self.payload,
            'expected_return_date': (self.today - timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {'expected_return_date'})
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.checkout_mock.assert_not_called()

    def test_insert_failure_rolls_back_without_registering_notification(self):
        def fail_insert(**kwargs):
            self.assertEqual(Book.objects.get(pk=self.book.pk).inventory, 1)
            raise IntegrityError('forced borrowing insert failure')

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with patch('borrowings.serializers.Borrowing.objects.create', side_effect=fail_insert):
                with self.assertRaisesMessage(IntegrityError, 'forced borrowing insert failure'):
                    self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(callbacks, [])
        self.notify_mock.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.checkout_mock.assert_not_called()

    def test_notification_waits_for_on_commit_callback(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(self.list_url, self.payload, format='json')
            self.notify_mock.assert_not_called()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(callbacks), 1)
        self.notify_mock.assert_not_called()

        def check_persisted_state(borrowing):
            self.assertEqual(borrowing.pk, response.data['id'])
            self.assertEqual(Payment.objects.filter(borrowing=borrowing).count(), 1)
            self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())
            self.assertEqual(Book.objects.get(pk=self.book.pk).inventory, 1)

        self.notify_mock.side_effect = check_persisted_state
        callbacks[0]()
        self.notify_mock.assert_called_once()

    def test_outer_rollback_discards_notification(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                response = self.client.post(self.list_url, self.payload, format='json')
                self.assertEqual(response.status_code, 201)
                self.notify_mock.assert_not_called()
                transaction.set_rollback(True)
        self.assertEqual(callbacks, [])
        self.notify_mock.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

    def test_telegram_failure_preserves_successful_creation(self):
        self.notify_mock.side_effect = notify_borrowing_created
        with patch(
            'notifications.services.send_telegram_message',
            side_effect=TelegramNotificationError('Telegram request failed.'),
        ) as send:
            with self.assertLogs('notifications.services', level='ERROR'):
                with self.captureOnCommitCallbacks(execute=True) as callbacks:
                    response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, BorrowingCreateSerializer(
            Borrowing.objects.get(pk=response.data['id']),
        ).data)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(len(callbacks), 1)
        self.notify_mock.assert_called_once()
        send.assert_called_once()
        self.assertTrue(Borrowing.objects.filter(pk=response.data['id']).exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

    def assert_invalid_amount(self, days, fee):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal(fee))
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(self.list_url, {
                **self.payload,
                'expected_return_date': (self.today + timedelta(days=days)).isoformat(),
            }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            'non_field_errors': ['Borrowing total must be at least $0.50.'],
        })
        self.assertEqual(callbacks, [])
        self.notify_mock.assert_not_called()
        self.checkout_mock.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

    def test_expected_return_today_is_rejected(self):
        self.assert_invalid_amount(0, '2.50')

    def test_zero_daily_fee_is_rejected(self):
        self.assert_invalid_amount(7, '0.00')

    def test_ten_cents_is_rejected(self):
        self.assert_invalid_amount(1, '0.10')

    def test_fifty_cents_is_accepted(self):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal('0.50'))
        response = self.client.post(self.list_url, {
            **self.payload,
            'expected_return_date': (self.today + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Payment.objects.get().money_to_pay, Decimal('0.50'))
        self.checkout_mock.assert_called_once()
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

    def test_missing_book_returns_400(self):
        response = self.client.post(self.list_url, {
            **self.payload, 'book': self.book.pk + 1,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {'book'})
        self.assertEqual(response.data['book'][0].code, 'does_not_exist')
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.checkout_mock.assert_not_called()

    def test_authenticated_user_cannot_update_or_delete_borrowing(self):
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        detail_url = reverse('borrowing-detail', args=[response.data['id']])
        original_borrowings = list(Borrowing.objects.order_by('pk').values())
        original_books = list(Book.objects.order_by('pk').values())
        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(detail_url, self.payload, format='json')
                self.assertEqual(response.status_code, 405)
                self.assertEqual(list(Borrowing.objects.order_by('pk').values()), original_borrowings)
                self.assertEqual(list(Book.objects.order_by('pk').values()), original_books)

    def test_success_creates_payment_and_uses_absolute_detail_urls(self):
        def checkout(borrowing, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())
            self.assertEqual(Book.objects.get(pk=self.book.pk).inventory, 1)
            self.assertFalse(Payment.objects.exists())
            self.notify_mock.assert_not_called()
            return self.session

        self.checkout_mock.side_effect = checkout
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(self.list_url, self.payload, format='json')
            self.notify_mock.assert_not_called()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Borrowing.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        borrowing = Borrowing.objects.get()
        payment = Payment.objects.get()
        expected_url = response.renderer_context['request'].build_absolute_uri(
            reverse('borrowing-detail', args=[borrowing.pk]),
        )
        self.checkout_mock.assert_called_once_with(
            borrowing, success_url=expected_url, cancel_url=expected_url,
        )
        self.assertEqual(payment.borrowing, borrowing)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.type, Payment.Type.PAYMENT)
        self.assertEqual(payment.money_to_pay, Decimal('17.50'))
        self.assertEqual(payment.session_id, self.session.id)
        self.assertEqual(payment.session_url, self.session.url)
        self.assertEqual(response.data['payments'], [PaymentSerializer(payment).data])
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)
        self.assertEqual(len(callbacks), 1)
        self.notify_mock.assert_called_once_with(borrowing)

    def assert_failed_creation_rolled_back(self):
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.notify_mock.assert_not_called()

    def test_stripe_failure_rolls_back_database(self):
        self.checkout_mock.side_effect = stripe.StripeError('forced Stripe failure')
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(stripe.StripeError):
                self.client.post(self.list_url, self.payload, format='json')
        self.checkout_mock.assert_called_once()
        self.assertEqual(callbacks, [])
        self.assert_failed_creation_rolled_back()

    def test_missing_configuration_rolls_back_database(self):
        self.checkout_mock.side_effect = ImproperlyConfigured('Stripe is not configured.')
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ImproperlyConfigured):
                self.client.post(self.list_url, self.payload, format='json')
        self.checkout_mock.assert_called_once()
        self.assertEqual(callbacks, [])
        self.assert_failed_creation_rolled_back()

    def test_payment_insert_failure_rolls_back_database(self):
        def fail_payment(borrowing, session):
            self.assertEqual(session, self.session)
            self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())
            self.assertEqual(Book.objects.get(pk=self.book.pk).inventory, 1)
            raise DatabaseError('forced Payment insert failure')

        with patch('borrowings.views.create_payment_for_borrowing', side_effect=fail_payment) as create:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(DatabaseError):
                    self.client.post(self.list_url, self.payload, format='json')
        create.assert_called_once()
        self.checkout_mock.assert_called_once()
        self.assertEqual(callbacks, [])
        self.assert_failed_creation_rolled_back()

    def test_nested_payment_payload_cannot_control_persistence(self):
        response = self.client.post(self.list_url, {
            **self.payload,
            'payments': [{
                'status': 'PAID', 'type': 'FINE', 'money_to_pay': '0.01',
                'session_id': 'injected', 'session_url': 'https://example.com/injected',
            }],
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Payment.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertEqual(payment.borrowing_id, response.data['id'])
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.type, Payment.Type.PAYMENT)
        self.assertEqual(payment.money_to_pay, Decimal('17.50'))
        self.assertEqual(payment.session_id, self.session.id)
        self.assertEqual(payment.session_url, self.session.url)
        self.assertEqual(response.data['payments'], [PaymentSerializer(payment).data])

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake')
    def test_real_checkout_service_wiring_uses_equivalent_amount(self):
        self.checkout_patch.stop()
        with patch(
            'payments.services.stripe.checkout.Session.create', return_value=self.session,
        ) as checkout:
            response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        checkout.assert_called_once()
        item = checkout.call_args.kwargs['line_items'][0]
        self.assertEqual(item['quantity'], 1)
        self.assertEqual(item['price_data']['unit_amount'], 1750)
        self.assertEqual(item['price_data']['currency'], 'usd')
        payment = Payment.objects.get()
        self.assertEqual(payment.money_to_pay, Decimal('17.50'))
        self.assertEqual(convert_to_stripe_amount(payment.money_to_pay), 1750)
        self.assertEqual(payment.session_id, self.session.id)
        self.assertEqual(response.data['payments'], [PaymentSerializer(payment).data])


class OverdueBorrowingTests(TestCase):
    def setUp(self):
        self.today = date(2026, 10, 20)
        date_patch = patch('borrowings.tasks.timezone.localdate', return_value=self.today)
        self.localdate = date_patch.start()
        self.addCleanup(date_patch.stop)
        self.user = get_user_model().objects.create_user(email='overdue@example.com')
        self.book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=5, daily_fee=Decimal('1.50'),
        )

    def create_borrowing(self, days, returned=False):
        return Borrowing.objects.create(
            user=self.user, book=self.book,
            borrow_date=self.today - timedelta(days=7),
            expected_return_date=self.today + timedelta(days=days),
            actual_return_date=self.today if returned else None,
        )

    def test_query_boundaries_and_order(self):
        today = self.create_borrowing(0)
        yesterday = self.create_borrowing(-1)
        yesterday_tie = self.create_borrowing(-1)
        self.create_borrowing(1)
        self.create_borrowing(-1, returned=True)
        self.create_borrowing(0, returned=True)
        self.assertEqual(list(get_overdue_borrowings()), [yesterday, yesterday_tie, today])
        self.localdate.assert_called_once_with()

    def test_related_book_and_user_load_in_one_query(self):
        for days in (-2, -1, 0):
            self.create_borrowing(days)
        with self.assertNumQueries(1):
            details = [
                (borrowing.book.title, borrowing.book.author, borrowing.user.email)
                for borrowing in get_overdue_borrowings()
            ]
        self.assertEqual(details, [('Book', 'Author', 'overdue@example.com')] * 3)

    def test_unreturned_overdue_remains_eligible_next_day(self):
        borrowing = self.create_borrowing(0)
        self.assertEqual(list(get_overdue_borrowings()), [borrowing])
        self.localdate.return_value = self.today + timedelta(days=1)
        self.assertEqual(list(get_overdue_borrowings()), [borrowing])

    @patch('borrowings.tasks.notify_no_overdue_borrowings')
    @patch('borrowings.tasks.notify_borrowing_overdue')
    def test_task_notifies_only_eligible_borrowings_in_order(self, notify, notify_empty):
        today = self.create_borrowing(0)
        yesterday = self.create_borrowing(-1)
        yesterday_tie = self.create_borrowing(-1)
        self.create_borrowing(1)
        self.create_borrowing(-1, returned=True)
        self.create_borrowing(0, returned=True)
        with self.assertNumQueries(1):
            self.assertIsNone(check_overdue_borrowings.run())
        self.assertEqual(notify.call_args_list, [call(yesterday), call(yesterday_tie), call(today)])
        notify_empty.assert_not_called()

    def assert_no_overdue_notification(self):
        with patch('borrowings.tasks.notify_borrowing_overdue') as notify:
            with patch('borrowings.tasks.notify_no_overdue_borrowings') as notify_empty:
                with self.assertNumQueries(1):
                    self.assertIsNone(check_overdue_borrowings.run())
                notify.assert_not_called()
                notify_empty.assert_called_once_with()

    def test_empty_database_sends_no_overdue_once(self):
        self.assert_no_overdue_notification()

    def test_only_future_sends_no_overdue_once(self):
        self.create_borrowing(1)
        self.assert_no_overdue_notification()

    def test_only_returned_sends_no_overdue_once(self):
        self.create_borrowing(-1, returned=True)
        self.create_borrowing(0, returned=True)
        self.assert_no_overdue_notification()

    @patch('borrowings.tasks.notify_no_overdue_borrowings')
    @patch('notifications.services.send_telegram_message')
    def test_transport_failure_does_not_stop_remaining_notifications(self, send, notify_empty):
        borrowings = [self.create_borrowing(days) for days in (-2, -1, 0)]
        send.side_effect = [TelegramNotificationError('Telegram request failed.'), None, None]
        with self.assertLogs('notifications.services', level='ERROR'):
            self.assertIsNone(check_overdue_borrowings.run())
        self.assertEqual(send.call_args_list, [
            call(format_overdue_borrowing_notification(borrowing)) for borrowing in borrowings
        ])
        notify_empty.assert_not_called()
