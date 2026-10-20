from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from payments.serializers import PaymentSerializer
from payments.services import (
    calculate_borrowing_price,
    convert_to_stripe_amount,
    create_checkout_session,
    create_payment_for_borrowing,
)


class BorrowingPriceTests(SimpleTestCase):
    def setUp(self):
        self.borrowing = Borrowing(
            book=Book(title='Book', inventory=5, daily_fee=Decimal('2.50')),
            borrow_date=date(2026, 10, 20),
            expected_return_date=date(2026, 10, 25),
        )

    def assert_price(self, expected):
        result = calculate_borrowing_price(self.borrowing)
        self.assertEqual(result, Decimal(expected))
        self.assertIsInstance(result, Decimal)

    def test_one_day(self):
        self.borrowing.expected_return_date = date(2026, 10, 21)
        self.assert_price('2.50')

    def test_multiple_days(self):
        self.assert_price('12.50')

    def test_same_day(self):
        self.borrowing.expected_return_date = self.borrowing.borrow_date
        self.assert_price('0.00')

    def test_zero_daily_fee(self):
        self.borrowing.book.daily_fee = Decimal('0.00')
        self.assert_price('0.00')

    def test_different_daily_fee(self):
        self.borrowing.expected_return_date = date(2026, 10, 23)
        self.borrowing.book.daily_fee = Decimal('1.75')
        self.assert_price('5.25')

    def test_actual_return_date_is_ignored(self):
        self.borrowing.actual_return_date = date(2026, 10, 21)
        self.assert_price('12.50')


class StripeAmountTests(SimpleTestCase):
    def test_decimal_amounts_convert_to_integer_minor_units(self):
        for amount, expected in (
            ('0.00', 0), ('0.01', 1), ('1.00', 100),
            ('5.50', 550), ('12.34', 1234),
        ):
            with self.subTest(amount=amount):
                result = convert_to_stripe_amount(Decimal(amount))
                self.assertEqual(result, expected)
                self.assertIs(type(result), int)

    def test_excess_decimal_places_are_rejected(self):
        with self.assertRaisesMessage(
            ValueError, 'Amount has more than 2 decimal places.',
        ):
            convert_to_stripe_amount(Decimal('5.555'))


@override_settings(STRIPE_SECRET_KEY='sk_test_fake')
class StripeCheckoutSessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(email='reader@example.com')
        cls.book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=5, daily_fee=Decimal('2.50'),
        )
        cls.borrowing = Borrowing.objects.create(
            user=user, book=cls.book, borrow_date=date(2026, 10, 20),
            expected_return_date=date(2026, 10, 25),
        )

    def setUp(self):
        create_patch = patch('payments.services.stripe.checkout.Session.create')
        self.mock_create = create_patch.start()
        self.addCleanup(create_patch.stop)
        self.success_url = 'https://example.com/success'
        self.cancel_url = 'https://example.com/cancel'

    def test_session_payload_return_and_domain_state(self):
        fake_session = object()
        self.mock_create.return_value = fake_session
        payment_count = Payment.objects.count()
        borrowing_state = Borrowing.objects.values().get(pk=self.borrowing.pk)
        self.assertEqual(self.book.inventory, 5)

        result = create_checkout_session(
            self.borrowing, self.success_url, self.cancel_url,
        )

        self.assertIs(result, fake_session)
        self.mock_create.assert_called_once()
        payload = self.mock_create.call_args.kwargs
        self.assertEqual(payload['api_key'], 'sk_test_fake')
        self.assertEqual(payload['mode'], 'payment')
        self.assertEqual(payload['success_url'], self.success_url)
        self.assertEqual(payload['cancel_url'], self.cancel_url)
        self.assertEqual(len(payload['line_items']), 1)
        item = payload['line_items'][0]
        self.assertEqual(item['quantity'], 1)
        self.assertEqual(item['price_data']['currency'], 'usd')
        self.assertEqual(item['price_data']['unit_amount'], 1250)
        self.assertIs(type(item['price_data']['unit_amount']), int)
        self.assertEqual(item['price_data']['product_data'], {
            'name': self.book.title,
        })
        self.assertEqual(Payment.objects.count(), payment_count)
        for field, value in borrowing_state.items():
            self.assertEqual(getattr(self.borrowing, field), value)
        self.assertEqual(
            Borrowing.objects.values().get(pk=self.borrowing.pk), borrowing_state,
        )
        self.assertEqual(self.borrowing.book.inventory, 5)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 5)

    def test_same_day_still_calls_stripe_with_zero_amount(self):
        self.borrowing.expected_return_date = self.borrowing.borrow_date

        create_checkout_session(self.borrowing, self.success_url, self.cancel_url)

        self.mock_create.assert_called_once()
        item = self.mock_create.call_args.kwargs['line_items'][0]
        self.assertEqual(item['price_data']['unit_amount'], 0)
        self.assertIs(type(item['price_data']['unit_amount']), int)
        self.assertEqual(item['quantity'], 1)

    @override_settings(STRIPE_SECRET_KEY='')
    def test_missing_secret_fails_before_stripe_call(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, 'Stripe is not configured.',
        ):
            create_checkout_session(
                self.borrowing, self.success_url, self.cancel_url,
            )
        self.mock_create.assert_not_called()


@override_settings(STRIPE_SECRET_KEY='sk_test_fake')
class CreatePaymentForBorrowingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='payment-helper@example.com')
        self.book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=5, daily_fee=Decimal('2.50'),
        )
        self.borrowing = Borrowing.objects.create(
            user=user, book=self.book, borrow_date=date(2026, 10, 20),
            expected_return_date=date(2026, 10, 27),
        )
        self.session = SimpleNamespace(
            id='cs_test_helper', url='https://checkout.stripe.com/c/pay/cs_test_helper',
        )

    @patch('payments.services.stripe.checkout.Session.create')
    def test_creates_and_returns_one_payment_without_other_side_effects(self, checkout):
        before = Borrowing.objects.values().get(pk=self.borrowing.pk)
        count = Payment.objects.count()
        with patch('notifications.services.send_telegram_message') as telegram:
            result = create_payment_for_borrowing(self.borrowing, self.session)
        self.assertEqual(Payment.objects.count(), count + 1)
        payment = Payment.objects.get()
        self.assertIsInstance(result, Payment)
        self.assertEqual(result, payment)
        self.assertEqual(payment.borrowing, self.borrowing)
        self.assertEqual(payment.money_to_pay, Decimal('17.50'))
        self.assertEqual(payment.session_id, self.session.id)
        self.assertEqual(payment.session_url, self.session.url)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.type, Payment.Type.PAYMENT)
        checkout.assert_not_called()
        telegram.assert_not_called()
        self.assertEqual(Borrowing.objects.values().get(pk=self.borrowing.pk), before)
        for field, value in before.items():
            self.assertEqual(getattr(self.borrowing, field), value)
        self.assertEqual(self.borrowing.book.inventory, 5)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 5)

    @patch('payments.services.stripe.checkout.Session.create')
    def test_payment_and_checkout_amounts_are_equivalent(self, checkout):
        checkout.return_value = self.session
        session = create_checkout_session(
            self.borrowing, 'https://example.com/borrowing', 'https://example.com/borrowing',
        )
        payment = create_payment_for_borrowing(self.borrowing, session)
        checkout.assert_called_once()
        item = checkout.call_args.kwargs['line_items'][0]
        self.assertEqual(item['price_data']['unit_amount'], 1750)
        self.assertEqual(
            convert_to_stripe_amount(payment.money_to_pay), item['price_data']['unit_amount'],
        )


class PaymentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(email='reader@example.com')
        book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=2, daily_fee=Decimal('1.50'),
        )
        cls.borrowing = Borrowing.objects.create(
            user=user, book=book, borrow_date=date(2026, 10, 1),
            expected_return_date=date(2026, 10, 20),
        )
        cls.payload = {
            'status': Payment.Status.PAID,
            'type': Payment.Type.PAYMENT,
            'borrowing': cls.borrowing,
            'session_url': 'https://example.com/checkout/session',
            'session_id': 'session-test',
            'money_to_pay': Decimal('12.50'),
        }

    def test_payment_persists_all_fields_with_decimal_money(self):
        payment = Payment(**self.payload)
        payment.full_clean()
        payment.save()
        payment.refresh_from_db()
        for field, expected in self.payload.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(payment, field), expected)
        self.assertIsInstance(payment.money_to_pay, Decimal)

    def test_status_defaults_to_pending(self):
        payload = {key: value for key, value in self.payload.items() if key != 'status'}
        payment = Payment(**payload)
        payment.full_clean()
        payment.save()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_status_and_type_choices(self):
        self.assertEqual(set(Payment.Status.values), {'PENDING', 'PAID'})
        self.assertEqual(set(Payment.Type.values), {'PAYMENT', 'FINE'})
        for status in Payment.Status.values:
            for payment_type in Payment.Type.values:
                with self.subTest(status=status, type=payment_type):
                    Payment(**{
                        **self.payload, 'status': status, 'type': payment_type,
                    }).full_clean()

    def test_invalid_status_and_type_fail_validation(self):
        for field in ('status', 'type'):
            with self.subTest(field=field):
                payment = Payment(**{**self.payload, field: 'INVALID'})
                with self.assertRaises(ValidationError) as caught:
                    payment.full_clean()
                self.assertEqual(set(caught.exception.message_dict), {field})

    def test_type_is_required_without_default(self):
        self.assertFalse(Payment._meta.get_field('type').has_default())
        payload = {key: value for key, value in self.payload.items() if key != 'type'}
        with self.assertRaises(ValidationError) as caught:
            Payment(**payload).full_clean()
        self.assertEqual(set(caught.exception.message_dict), {'type'})

    def test_session_fields_can_be_omitted(self):
        payload = {
            key: value for key, value in self.payload.items()
            if key not in ('session_url', 'session_id')
        }
        payment = Payment(**payload)
        payment.full_clean()
        payment.save()
        payment.refresh_from_db()
        self.assertEqual(payment.session_url, '')
        self.assertEqual(payment.session_id, '')

    def test_session_id_can_be_reused(self):
        for _ in range(2):
            payment = Payment(**self.payload)
            payment.full_clean()
            payment.save()
        self.assertEqual(Payment.objects.filter(session_id='session-test').count(), 2)

    def test_zero_money_is_valid(self):
        payment = Payment(**{**self.payload, 'money_to_pay': Decimal('0.00')})
        payment.full_clean()
        payment.save()
        payment.refresh_from_db()
        self.assertEqual(payment.money_to_pay, Decimal('0.00'))
        self.assertIsInstance(payment.money_to_pay, Decimal)

    def test_negative_money_fails_validation(self):
        payment = Payment(**{**self.payload, 'money_to_pay': Decimal('-0.01')})
        with self.assertRaises(ValidationError) as caught:
            payment.full_clean()
        self.assertEqual(set(caught.exception.message_dict), {'money_to_pay'})

    def test_money_with_excess_decimal_places_fails_validation(self):
        payment = Payment(**{**self.payload, 'money_to_pay': Decimal('12.501')})
        with self.assertRaises(ValidationError) as caught:
            payment.full_clean()
        self.assertEqual(set(caught.exception.message_dict), {'money_to_pay'})

    def test_borrowing_reverse_relation_accepts_payment_and_fine(self):
        payment = Payment.objects.create(**self.payload)
        fine = Payment.objects.create(**{**self.payload, 'type': Payment.Type.FINE})
        self.assertEqual(list(self.borrowing.payments.order_by('id')), [payment, fine])

    def test_payment_protects_borrowing_from_deletion(self):
        payment = Payment.objects.create(**self.payload)
        borrowing_pk = self.borrowing.pk
        with self.assertRaises(ProtectedError):
            self.borrowing.delete()
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertTrue(Borrowing.objects.filter(pk=borrowing_pk).exists())


class PaymentSerializerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(email='reader@example.com')
        book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=2, daily_fee=Decimal('1.50'),
        )
        borrowing = Borrowing.objects.create(
            user=user, book=book, borrow_date=date(2026, 10, 1),
            expected_return_date=date(2026, 10, 20),
        )
        cls.payment = Payment.objects.create(
            borrowing=borrowing, type=Payment.Type.PAYMENT,
            money_to_pay=Decimal('12.50'),
            session_url='https://example.com/checkout/session',
            session_id='session-test',
        )

    def test_exact_fields(self):
        self.assertEqual(set(PaymentSerializer().fields), {
            'id', 'status', 'type', 'borrowing',
            'session_url', 'session_id', 'money_to_pay',
        })

    def test_complete_output_preserves_pk_decimal_and_session_fields(self):
        for session_url, session_id in (
            ('https://example.com/checkout/session', 'session-test'),
            ('', ''),
        ):
            with self.subTest(session_id=session_id):
                self.payment.session_url = session_url
                self.payment.session_id = session_id
                self.payment.save()
                self.payment.refresh_from_db()
                data = PaymentSerializer(self.payment).data
                self.assertEqual(data, {
                    'id': self.payment.pk,
                    'status': 'PENDING',
                    'type': 'PAYMENT',
                    'borrowing': self.payment.borrowing_id,
                    'session_url': session_url,
                    'session_id': session_id,
                    'money_to_pay': '12.50',
                })
                self.assertIsInstance(data['borrowing'], int)
                self.assertIsInstance(data['money_to_pay'], str)

    def test_all_fields_are_read_only(self):
        for name, field in PaymentSerializer().fields.items():
            with self.subTest(field=name):
                self.assertTrue(field.read_only)


class PaymentAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = 'payments-test-password'
        for name in ('user_a', 'user_b', 'user_c', 'staff'):
            setattr(cls, name, get_user_model().objects.create_user(
                email=f'{name}@example.com', password=cls.password,
                is_staff=name == 'staff', is_superuser=False,
            ))
        book = Book.objects.create(
            title='Book', author='Author', cover=Book.Cover.HARD,
            inventory=2, daily_fee=Decimal('1.50'),
        )
        borrowings = [
            Borrowing.objects.create(
                user=user, book=book, borrow_date=date(2026, 10, 1),
                expected_return_date=date(2026, 10, 20),
            )
            for user in (cls.user_a, cls.user_b)
        ]
        # Explicit, out-of-order IDs ensure ordering is tested independently of creation.
        cls.payment_a = Payment.objects.create(
            id=30, borrowing=borrowings[0], type=Payment.Type.PAYMENT,
            money_to_pay=Decimal('12.50'),
            session_url='https://example.com/checkout/session',
            session_id='session-test',
        )
        cls.fine_a = Payment.objects.create(
            id=10, borrowing=borrowings[0], type=Payment.Type.FINE,
            status=Payment.Status.PAID, money_to_pay=Decimal('3.00'),
        )
        cls.payment_b = Payment.objects.create(
            id=20, borrowing=borrowings[1], type=Payment.Type.PAYMENT,
            money_to_pay=Decimal('7.25'),
        )

    def setUp(self):
        self.list_url = reverse('payment-list')
        self.detail_url = reverse('payment-detail', args=[self.payment_a.pk])
        self.foreign_url = reverse('payment-detail', args=[self.payment_b.pk])
        self.missing_url = reverse('payment-detail', args=[999])
        self.assertFalse(Payment.objects.filter(pk=999).exists())

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

    def test_anonymous_list_requires_authentication(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_anonymous_detail_requires_authentication(self):
        self.assertEqual(self.client.get(self.detail_url).status_code, 401)

    def test_anonymous_missing_detail_requires_authentication(self):
        self.assertEqual(self.client.get(self.missing_url).status_code, 401)

    def test_anonymous_write_methods_require_authentication(self):
        original = list(Payment.objects.order_by('id').values())
        for method, url in (
            ('post', self.list_url), ('put', self.detail_url),
            ('patch', self.detail_url), ('delete', self.detail_url),
        ):
            with self.subTest(method=method):
                response = getattr(self.client, method)(url, {}, format='json')
                self.assertEqual(response.status_code, 401)
                self.assertEqual(list(Payment.objects.order_by('id').values()), original)

    def test_user_lists_only_own_payments_in_id_order_with_custom_header(self):
        self.authenticate(self.user_a)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.renderer_context['request'].user, self.user_a)
        self.assertIsInstance(response.data, list)
        self.assertEqual([item['id'] for item in response.data], [10, 30])
        expected = Payment.objects.filter(borrowing__user=self.user_a).order_by('id')
        self.assertEqual(response.data, PaymentSerializer(expected, many=True).data)
        self.assertNotIn(self.payment_b.pk, [item['id'] for item in response.data])

    def test_user_can_retrieve_own_payments(self):
        self.authenticate(self.user_a)
        for payment in (self.payment_a, self.fine_a):
            with self.subTest(payment=payment.pk):
                response = self.client.get(reverse('payment-detail', args=[payment.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, PaymentSerializer(payment).data)

    def test_foreign_detail_returns_404(self):
        self.authenticate(self.user_a)
        response = self.client.get(self.foreign_url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.data), {'detail'})

    def test_staff_non_superuser_lists_all_payments_in_id_order(self):
        self.assertTrue(self.staff.is_staff)
        self.assertFalse(self.staff.is_superuser)
        self.authenticate(self.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data], [10, 20, 30])
        self.assertEqual(response.data, PaymentSerializer(
            Payment.objects.order_by('id'), many=True,
        ).data)

    def test_staff_non_superuser_retrieves_payments_of_both_users(self):
        self.assertTrue(self.staff.is_staff)
        self.assertFalse(self.staff.is_superuser)
        self.authenticate(self.staff)
        for payment in (self.payment_a, self.payment_b):
            with self.subTest(payment=payment.pk):
                response = self.client.get(reverse('payment-detail', args=[payment.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, PaymentSerializer(payment).data)

    def test_user_without_payments_receives_empty_list(self):
        self.authenticate(self.user_c)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_authenticated_missing_detail_returns_404(self):
        self.authenticate(self.user_a)
        response = self.client.get(self.missing_url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.data), {'detail'})

    def test_authenticated_write_methods_return_405_without_changes(self):
        original = list(Payment.objects.order_by('id').values())
        payload = {
            'status': 'PAID', 'type': 'FINE',
            'borrowing': self.payment_b.borrowing_id,
            'session_url': 'https://example.com/changed',
            'session_id': 'changed', 'money_to_pay': '99.99',
        }
        for user in (self.user_a, self.staff):
            self.authenticate(user)
            for method, url in (
                ('post', self.list_url), ('put', self.detail_url),
                ('patch', self.detail_url), ('delete', self.detail_url),
            ):
                with self.subTest(user=user.email, method=method):
                    response = getattr(self.client, method)(url, payload, format='json')
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(list(Payment.objects.order_by('id').values()), original)

    def test_standard_authorization_header_does_not_authenticate(self):
        access = self.authenticate(self.user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        for url in (self.list_url, self.detail_url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_route_paths(self):
        self.assertEqual(self.list_url, '/payments/')
        self.assertEqual(self.detail_url, f'/payments/{self.payment_a.pk}/')
