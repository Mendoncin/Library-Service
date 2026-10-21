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


def create_checkout_session(borrowing, success_url, cancel_url):
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured('Stripe is not configured.')

    unit_amount = convert_to_stripe_amount(calculate_borrowing_price(borrowing))
    return stripe.checkout.Session.create(
        api_key=settings.STRIPE_SECRET_KEY,
        mode='payment',
        line_items=[
            {
                'price_data': {
                    'currency': STRIPE_CURRENCY,
                    'unit_amount': unit_amount,
                    'product_data': {
                        'name': borrowing.book.title,
                    },
                },
                'quantity': 1,
            },
        ],
        success_url=success_url,
        cancel_url=cancel_url,
    )
