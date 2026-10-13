from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from books.models import Book
from borrowings.models import Borrowing


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
