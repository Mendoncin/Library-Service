from decimal import Decimal

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from payments.models import Payment


STRIPE_CURRENCY = 'usd'
STRIPE_MIN_AMOUNT = 50


def retrieve_checkout_session(session_id: str):
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured('Stripe is not configured.')

    return stripe.checkout.Session.retrieve(
        session_id,
        api_key=settings.STRIPE_SECRET_KEY,
    )


def calculate_borrowing_price(borrowing) -> Decimal:
    days = (borrowing.expected_return_date - borrowing.borrow_date).days
    return days * borrowing.book.daily_fee


def convert_to_stripe_amount(amount: Decimal) -> int:
    minor = amount * Decimal('100')
    if minor != minor.to_integral_value():
        raise ValueError('Amount has more than 2 decimal places.')
    return int(minor)


def create_payment_for_borrowing(borrowing, session) -> Payment:
    return Payment.objects.create(
        borrowing=borrowing,
        money_to_pay=calculate_borrowing_price(borrowing),
        session_url=session.url,
        session_id=session.id,
        status=Payment.Status.PENDING,
        type=Payment.Type.PAYMENT,
    )


def calculate_overdue_days(borrowing, return_date) -> int:
    return max((return_date - borrowing.expected_return_date).days, 0)


def calculate_fine_amount(borrowing, return_date) -> Decimal:
    return (
        calculate_overdue_days(borrowing, return_date)
        * borrowing.book.daily_fee
        * settings.FINE_MULTIPLIER
    )


def create_fine_payment(borrowing, amount, session=None) -> Payment:
    return Payment.objects.create(
        borrowing=borrowing,
        money_to_pay=amount,
        session_url=session.url if session is not None else '',
        session_id=session.id if session is not None else '',
        status=Payment.Status.PENDING,
        type=Payment.Type.FINE,
    )


def create_checkout_session(borrowing, success_url, cancel_url):
    return _create_stripe_checkout_session(
        calculate_borrowing_price(borrowing), borrowing.book.title,
        success_url, cancel_url,
    )


def create_fine_checkout_session(borrowing, amount, success_url, cancel_url):
    return _create_stripe_checkout_session(
        amount, f'Fine for {borrowing.book.title}', success_url, cancel_url,
    )


def _create_stripe_checkout_session(amount, product_name, success_url, cancel_url):
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured('Stripe is not configured.')

    unit_amount = convert_to_stripe_amount(amount)
    return stripe.checkout.Session.create(
        api_key=settings.STRIPE_SECRET_KEY,
        mode='payment',
        line_items=[
            {
                'price_data': {
                    'currency': STRIPE_CURRENCY,
                    'unit_amount': unit_amount,
                    'product_data': {
                        'name': product_name,
                    },
                },
                'quantity': 1,
            },
        ],
        success_url=success_url,
        cancel_url=cancel_url,
    )
