from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import resolve, reverse
from rest_framework.exceptions import ValidationError as SerializerValidationError
from rest_framework.test import APITestCase

from books.models import Book
from books.serializers import BookSerializer
from borrowings.models import Borrowing
from borrowings.serializers import BorrowingCreateSerializer, BorrowingSerializer
from borrowings.urls import urlpatterns
from borrowings.views import BorrowingViewSet


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
        self.borrowings = []
        for index, actual_return_date in enumerate((None, date(2026, 10, 10))):
            book = Book.objects.create(
                title=f'Book {index}',
                author=f'Author {index}',
                cover=Book.Cover.HARD,
                inventory=index + 1,
                daily_fee=Decimal('2.50'),
            )
            user = get_user_model().objects.create_user(email=f'reader{index}@example.com')
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
                })

    def test_serializer_fields_are_read_only(self):
        serializer = BorrowingSerializer()
        self.assertIsInstance(serializer.fields['book'], BookSerializer)
        for name, field in serializer.fields.items():
            with self.subTest(field=name):
                self.assertTrue(field.read_only)

    def test_anonymous_can_list_borrowings_with_detailed_books(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data, BorrowingSerializer(self.borrowings, many=True).data)
        for item, borrowing in zip(response.data, self.borrowings):
            self.assertIsInstance(item['book'], dict)
            self.assertEqual(item['book'], BookSerializer(borrowing.book).data)
            self.assertEqual(item['user'], borrowing.user_id)

    def test_anonymous_can_retrieve_borrowing(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, BorrowingSerializer(self.borrowing).data)

    def test_missing_borrowing_returns_404(self):
        url = reverse('borrowing-detail', args=[self.borrowings[-1].pk + 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_update_and_delete_are_not_allowed(self):
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

    def test_routes_expose_only_list_retrieve_and_create(self):
        self.assertEqual(self.list_url, '/borrowings/')
        self.assertEqual(self.detail_url, f'/borrowings/{self.borrowing.pk}/')
        self.assertEqual(len(urlpatterns), 2)
        self.assertEqual({pattern.name for pattern in urlpatterns}, {
            'borrowing-list', 'borrowing-detail',
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
        self.assertEqual(set(serializer.fields), {'id', 'book', 'expected_return_date'})
        self.assertTrue(serializer.fields['id'].read_only)
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
        self.assertEqual(serializer.data, {'id': borrowing.pk, **self.payload})

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

    def test_expected_return_today_is_valid(self):
        serializer = BorrowingCreateSerializer(data={
            **self.payload, 'expected_return_date': self.today.isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        borrowing = serializer.save(user=self.user)
        borrowing.refresh_from_db()
        self.assertEqual(borrowing.expected_return_date, borrowing.borrow_date)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

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
            **self.payload, 'expected_return_date': self.today.isoformat(),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.localdate.return_value = self.today + timedelta(days=1)
        with self.assertRaises(SerializerValidationError) as caught:
            serializer.save(user=self.user)
        self.assertEqual(caught.exception.detail, {
            'expected_return_date': 'Expected return date cannot be before today.',
        })
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)
        self.assertFalse(Borrowing.objects.exists())

    def test_book_and_expected_return_date_are_required(self):
        serializer = BorrowingCreateSerializer(data={})
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {'book', 'expected_return_date'})
        for errors in serializer.errors.values():
            self.assertEqual(errors[0].code, 'required')


class BorrowingCreateAPITests(APITestCase):
    def setUp(self):
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
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.renderer_context['request'].user, self.user)
        self.assertEqual(Borrowing.objects.count(), 1)
        borrowing = Borrowing.objects.get(pk=response.data['id'])
        self.assertEqual(response.data, {'id': borrowing.pk, **self.payload})
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

    def test_anonymous_cannot_create_borrowing(self):
        self.client.credentials()
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Borrowing.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

    def test_out_of_stock_returns_400_without_creating_borrowing(self):
        Book.objects.filter(pk=self.book.pk).update(inventory=0)
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'book': ['This book is out of stock.']})
        self.assertFalse(Borrowing.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 0)

    def test_past_expected_return_returns_400_without_decreasing_inventory(self):
        response = self.client.post(self.list_url, {
            **self.payload,
            'expected_return_date': (self.today - timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {'expected_return_date'})
        self.assertFalse(Borrowing.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

    def test_expected_return_today_is_accepted(self):
        response = self.client.post(self.list_url, {
            **self.payload, 'expected_return_date': self.today.isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 201)
        borrowing = Borrowing.objects.get(pk=response.data['id'])
        self.assertEqual(borrowing.expected_return_date, self.today)
        self.assertEqual(borrowing.borrow_date, self.today)
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
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

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
