from celery import shared_task
from django.utils import timezone

from borrowings.models import Borrowing
from notifications.services import notify_borrowing_overdue, notify_no_overdue_borrowings


def get_overdue_borrowings():
    today = timezone.localdate()
    return (
        Borrowing.objects.filter(
            expected_return_date__lte=today,
            actual_return_date__isnull=True,
        )
        .select_related('book', 'user')
        .order_by('expected_return_date', 'id')
    )


@shared_task
def check_overdue_borrowings():
    overdue_borrowings = list(get_overdue_borrowings())
    if not overdue_borrowings:
        notify_no_overdue_borrowings()
        return None

    for borrowing in overdue_borrowings:
        notify_borrowing_overdue(borrowing)

    return None
