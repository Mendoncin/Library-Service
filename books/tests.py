from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework.test import APITestCase

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


class BookAPITests(APITestCase):
    def setUp(self):
        self.payload = {
            'title': 'Book',
            'author': 'Author',
            'cover': 'HARD',
            'inventory': 1,
            'daily_fee': '1.50',
        }
        self.book = Book.objects.create(**self.payload)
        self.list_url = reverse('book-list')
        self.detail_url = reverse('book-detail', args=[self.book.pk])

    def test_create_book(self):
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Book.objects.count(), 2)
        created = Book.objects.get(pk=response.data['id'])
        self.assertNotEqual(created.pk, self.book.pk)
        self.assertEqual(response.data, {'id': created.pk, **self.payload})
        self.assertEqual(created.title, self.payload['title'])
        self.assertEqual(created.author, self.payload['author'])
        self.assertEqual(created.cover, self.payload['cover'])
        self.assertEqual(created.inventory, self.payload['inventory'])
        self.assertEqual(created.daily_fee, Decimal('1.50'))

    def test_list_books(self):
        second_payload = {**self.payload, 'title': 'Second book', 'cover': 'SOFT'}
        second = Book.objects.create(**second_payload)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(response.data, [
            {'id': self.book.pk, **self.payload},
            {'id': second.pk, **second_payload},
        ])

    def test_retrieve_book(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'id': self.book.pk, **self.payload})

    def test_missing_book_returns_404(self):
        url = reverse('book-detail', args=[self.book.pk + 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_update_book(self):
        payload = {
            'title': 'Updated book',
            'author': 'Updated author',
            'cover': 'SOFT',
            'inventory': 3,
            'daily_fee': '2.75',
        }
        response = self.client.put(self.detail_url, payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'id': self.book.pk, **payload})
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, payload['title'])
        self.assertEqual(self.book.author, payload['author'])
        self.assertEqual(self.book.cover, payload['cover'])
        self.assertEqual(self.book.inventory, payload['inventory'])
        self.assertEqual(self.book.daily_fee, Decimal('2.75'))

    def test_partial_update_preserves_other_fields(self):
        response = self.client.patch(self.detail_url, {'inventory': 0}, format='json')
        self.assertEqual(response.status_code, 200)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 0)
        self.assertEqual(self.book.title, self.payload['title'])
        self.assertEqual(self.book.author, self.payload['author'])
        self.assertEqual(self.book.cover, self.payload['cover'])
        self.assertEqual(self.book.daily_fee, Decimal('1.50'))

    def test_delete_book(self):
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertFalse(Book.objects.filter(pk=self.book.pk).exists())

    def test_invalid_payload_returns_field_errors(self):
        for field, value in (
            ('inventory', -1),
            ('daily_fee', '-0.01'),
            ('cover', 'INVALID'),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    self.list_url, {**self.payload, field: value}, format='json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(set(response.data), {field})
                self.assertEqual(Book.objects.count(), 1)

    def test_route_paths(self):
        self.assertEqual(self.list_url, '/books/')
        self.assertEqual(self.detail_url, f'/books/{self.book.pk}/')

    def test_browsable_api_renders(self):
        response = self.client.get(self.list_url, HTTP_ACCEPT='text/html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
