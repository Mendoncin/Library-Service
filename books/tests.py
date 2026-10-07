from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from books.models import Book
from books.serializers import BookSerializer


class BookValidationTests(SimpleTestCase):
    def setUp(self):
        self.book = Book(
            title='Book',
            author='Author',
            cover=Book.Cover.HARD,
            inventory=1,
            daily_fee=Decimal('1.50'),
        )

    def test_nonnegative_inventory_is_valid(self):
        for inventory in (0, 1):
            with self.subTest(inventory=inventory):
                self.book.inventory = inventory
                self.book.full_clean()

    def test_negative_inventory_is_invalid(self):
        self.book.inventory = -1
        with self.assertRaises(ValidationError) as caught:
            self.book.full_clean()
        self.assertEqual(set(caught.exception.error_dict), {'inventory'})
        self.assertEqual(caught.exception.error_dict['inventory'][0].code, 'min_value')

    def test_nonnegative_daily_fee_is_valid(self):
        for daily_fee in (Decimal('0.00'), Decimal('1.50')):
            with self.subTest(daily_fee=daily_fee):
                self.book.daily_fee = daily_fee
                self.book.full_clean()

    def test_negative_daily_fee_is_invalid(self):
        self.book.daily_fee = Decimal('-0.01')
        with self.assertRaises(ValidationError) as caught:
            self.book.full_clean()
        self.assertEqual(set(caught.exception.error_dict), {'daily_fee'})
        self.assertEqual(caught.exception.error_dict['daily_fee'][0].code, 'min_value')

    def test_cover_choices_are_valid(self):
        for cover in ('HARD', 'SOFT'):
            with self.subTest(cover=cover):
                self.book.cover = cover
                self.book.full_clean()

    def test_unknown_cover_is_invalid(self):
        self.book.cover = 'INVALID'
        with self.assertRaises(ValidationError) as caught:
            self.book.full_clean()
        self.assertEqual(set(caught.exception.error_dict), {'cover'})
        self.assertEqual(caught.exception.error_dict['cover'][0].code, 'invalid_choice')


class BookSerializerTests(SimpleTestCase):
    def setUp(self):
        self.payload = {
            'title': 'Book',
            'author': 'Author',
            'cover': 'HARD',
            'inventory': 1,
            'daily_fee': '1.50',
        }

    def test_valid_payload_with_each_cover(self):
        for cover in ('HARD', 'SOFT'):
            with self.subTest(cover=cover):
                serializer = BookSerializer(data={**self.payload, 'cover': cover})
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data['cover'], cover)
                self.assertEqual(serializer.validated_data['daily_fee'], Decimal('1.50'))
                self.assertIsInstance(serializer.validated_data['daily_fee'], Decimal)

    def test_zero_inventory_and_daily_fee_are_valid(self):
        serializer = BookSerializer(data={
            **self.payload,
            'inventory': 0,
            'daily_fee': '0.00',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['inventory'], 0)
        self.assertEqual(serializer.validated_data['daily_fee'], Decimal('0.00'))

    def test_invalid_values_produce_field_errors(self):
        cases = (
            ('inventory', -1, 'min_value'),
            ('daily_fee', '-0.01', 'min_value'),
            ('cover', 'INVALID', 'invalid_choice'),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                serializer = BookSerializer(data={**self.payload, field: value})
                self.assertFalse(serializer.is_valid())
                self.assertEqual(set(serializer.errors), {field})
                self.assertEqual(serializer.errors[field][0].code, code)

    def test_representation_exposes_exact_fields(self):
        book = Book(id=7, **{**self.payload, 'daily_fee': Decimal('1.50')})
        self.assertEqual(BookSerializer(book).data, {
            'id': 7,
            'title': 'Book',
            'author': 'Author',
            'cover': 'HARD',
            'inventory': 1,
            'daily_fee': '1.50',
        })

    def test_id_is_read_only(self):
        serializer = BookSerializer(data={**self.payload, 'id': 99})
        self.assertTrue(serializer.fields['id'].read_only)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn('id', serializer.validated_data)
