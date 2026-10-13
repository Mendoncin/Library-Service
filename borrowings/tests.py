from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import resolve, reverse
from rest_framework.test import APITestCase

from books.models import Book
from books.serializers import BookSerializer
from borrowings.models import Borrowing
from borrowings.serializers import BorrowingSerializer
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

    def test_write_methods_are_not_allowed(self):
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
            ('post', self.list_url),
            ('put', self.detail_url),
            ('patch', self.detail_url),
            ('delete', self.detail_url),
        ):
            with self.subTest(method=method):
                response = getattr(self.client, method)(url, payload, format='json')
                self.assertEqual(response.status_code, 405)
                self.assertEqual(list(Borrowing.objects.order_by('pk').values()), original_borrowings)
                self.assertEqual(list(Book.objects.order_by('pk').values()), original_books)

    def test_routes_expose_only_list_and_retrieve(self):
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
                self.assertLessEqual(set(match.func.actions), {'get', 'head'})
