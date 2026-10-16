import requests
from django.conf import settings


TELEGRAM_TIMEOUT_SECONDS = 5


class TelegramNotificationError(Exception):
    pass


def send_telegram_message(message: str) -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        raise TelegramNotificationError('Telegram is not configured.')

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        response = requests.post(
            url,
            json={'chat_id': chat_id, 'text': message},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise TelegramNotificationError('Telegram request failed.') from None

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise TelegramNotificationError(
            f'Telegram returned HTTP {response.status_code}.',
        ) from None
    except requests.RequestException:
        raise TelegramNotificationError('Telegram request failed.') from None
