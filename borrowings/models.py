from django.conf import settings
from django.db import models


class Borrowing(models.Model):
    borrow_date = models.DateField()
    expected_return_date = models.DateField()
    actual_return_date = models.DateField(null=True, blank=True)
    book = models.ForeignKey('books.Book', on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expected_return_date__gte=models.F('borrow_date')),
                name='borrowing_expected_return_not_before_borrow',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(actual_return_date__isnull=True)
                    | models.Q(actual_return_date__gte=models.F('borrow_date'))
                ),
                name='borrowing_actual_return_not_before_borrow',
            ),
        ]
