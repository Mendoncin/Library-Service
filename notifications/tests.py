import traceback
from datetime import date
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from books.models import Book
from borrowings.models import Borrowing
from notifications.services import format_borrowing_notification, notify_borrowing_created
from notifications.telegram import TelegramNotificationError, send_telegram_message


def borrowing_fixture():
    return Borrowing(
        pk=7,
        user=get_user_model()(email='reader@example.com'),
        book=Book(title='Clean Code', author='Robert C. Martin'),
        borrow_date=date(2026, 10, 13),
        expected_return_date=date(2026, 10, 20),
    )


EXPECTED_MESSAGE = (
    'New borrowing created\n\n'
    'Borrowing ID: 7\n'
    'User: reader@example.com\n'
    'Book: Clean Code by Robert C. Martin\n'
    'Borrow date: 2026-10-13\n'
    'Expected return date: 2026-10-20'
)


@override_settings(TELEGRAM_BOT_TOKEN='test-token', TELEGRAM_CHAT_ID='12345')
class TelegramClientTests(SimpleTestCase):
    def setUp(self):
        patcher = patch('notifications.telegram.requests.post')
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_sanitized(self, exception):
        self.assertNotIn('test-token', str(exception))
        self.assertNotIn('test-token', ''.join(traceback.format_exception(exception)))
        self.assertTrue(exception.__suppress_context__)

    def test_success(self):
        self.assertIsNone(send_telegram_message('hello'))
        self.post.assert_called_once_with(
            'https://api.telegram.org/bottest-token/sendMessage',
            json={'chat_id': '12345', 'text': 'hello'},
            timeout=5,
        )
        self.post.return_value.raise_for_status.assert_called_once_with()

    def test_http_errors_are_sanitized(self):
        for status_code in (401, 429, 500):
            with self.subTest(status_code=status_code):
                self.post.return_value.status_code = status_code
                self.post.return_value.raise_for_status.side_effect = requests.HTTPError(
                    'https://api.telegram.org/bottest-token/sendMessage',
                )
                with self.assertRaisesMessage(
                    TelegramNotificationError, f'Telegram returned HTTP {status_code}.',
                ) as caught:
                    send_telegram_message('hello')
                self.assert_sanitized(caught.exception)

    def test_network_errors_are_sanitized(self):
        for error in (requests.Timeout, requests.ConnectionError, requests.RequestException):
            with self.subTest(error=error):
                self.post.side_effect = error('something containing bottest-token')
                with self.assertRaises(TelegramNotificationError) as caught:
                    send_telegram_message('hello')
                self.assert_sanitized(caught.exception)

    def test_missing_token(self):
        with override_settings(TELEGRAM_BOT_TOKEN=''):
            with self.assertRaisesMessage(TelegramNotificationError, 'Telegram is not configured.'):
                send_telegram_message('hello')
        self.post.assert_not_called()

    def test_missing_chat_id(self):
        with override_settings(TELEGRAM_CHAT_ID=''):
            with self.assertRaisesMessage(TelegramNotificationError, 'Telegram is not configured.'):
                send_telegram_message('hello')
        self.post.assert_not_called()


@override_settings(TELEGRAM_BOT_TOKEN='test-token', TELEGRAM_CHAT_ID='12345')
class NotificationServiceTests(SimpleTestCase):
    def setUp(self):
        self.borrowing = borrowing_fixture()
        patcher = patch('notifications.services.send_telegram_message')
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_formatter_exact_output(self):
        self.assertEqual(format_borrowing_notification(self.borrowing), EXPECTED_MESSAGE)
        self.send.assert_not_called()

    def test_success_sends_formatted_message_once(self):
        self.assertIsNone(notify_borrowing_created(self.borrowing))
        self.send.assert_called_once_with(EXPECTED_MESSAGE)

    def test_send_failure_is_logged_and_not_propagated(self):
        self.send.side_effect = TelegramNotificationError('Telegram request failed.')
        with self.assertLogs('notifications.services', level='ERROR'):
            self.assertIsNone(notify_borrowing_created(self.borrowing))
        self.send.assert_called_once_with(EXPECTED_MESSAGE)

    def test_formatter_failure_is_logged_and_not_propagated(self):
        with patch('notifications.services.format_borrowing_notification', side_effect=ValueError):
            with self.assertLogs('notifications.services', level='ERROR'):
                self.assertIsNone(notify_borrowing_created(self.borrowing))
        self.send.assert_not_called()

    def test_real_client_failure_does_not_leak_token_in_logs(self):
        self.send.side_effect = send_telegram_message
        with patch('notifications.telegram.requests.post', side_effect=requests.Timeout('bottest-token')):
            with self.assertLogs('notifications.services', level='ERROR') as logs:
                self.assertIsNone(notify_borrowing_created(self.borrowing))
        self.assertNotIn('test-token', '\n'.join(logs.output))


@override_settings(TELEGRAM_BOT_TOKEN='test-token', TELEGRAM_CHAT_ID='12345')
class PersistedBorrowingNotificationTests(TestCase):
    @patch('notifications.services.send_telegram_message')
    def test_message_uses_persisted_borrowing(self, send):
        user = get_user_model().objects.create_user(email='reader@example.com')
        book = Book.objects.create(
            title='Clean Code', author='Robert C. Martin', cover=Book.Cover.HARD,
            inventory=2, daily_fee='1.50',
        )
        borrowing = Borrowing.objects.create(
            user=user, book=book, borrow_date=date(2026, 10, 13),
            expected_return_date=date(2026, 10, 20),
        )
        borrowing.refresh_from_db()
        self.assertIsNone(notify_borrowing_created(borrowing))
        send.assert_called_once_with(
            EXPECTED_MESSAGE.replace('Borrowing ID: 7', f'Borrowing ID: {borrowing.pk}'),
        )
