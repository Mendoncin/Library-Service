from decimal import Decimal

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


STRIPE_CURRENCY = 'usd'


def calculate_borrowing_price(borrowing) -> Decimal:
    days = (borrowing.expected_return_date - borrowing.borrow_date).days
    return days * borrowing.book.daily_fee


def convert_to_stripe_amount(amount: Decimal) -> int:
    minor = amount * Decimal('100')
    if minor != minor.to_integral_value():
        raise ValueError('Amount has more than 2 decimal places.')
    return int(minor)


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
