import logging

from notifications.telegram import send_telegram_message


logger = logging.getLogger(__name__)


def format_borrowing_notification(borrowing) -> str:
    return '\n'.join([
        'New borrowing created',
        '',
        f'Borrowing ID: {borrowing.pk}',
        f'User: {borrowing.user.email}',
        f'Book: {borrowing.book.title} by {borrowing.book.author}',
        f'Borrow date: {borrowing.borrow_date.isoformat()}',
        f'Expected return date: {borrowing.expected_return_date.isoformat()}',
    ])


def notify_borrowing_created(borrowing) -> None:
    try:
        message = format_borrowing_notification(borrowing)
        send_telegram_message(message)
    except Exception:
        logger.exception(
            'Failed to send borrowing notification (borrowing_id=%s).',
            borrowing.pk,
        )
