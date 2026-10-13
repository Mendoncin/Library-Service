from rest_framework import serializers

from books.serializers import BookSerializer
from borrowings.models import Borrowing


class BorrowingSerializer(serializers.ModelSerializer):
    book = BookSerializer(read_only=True)

    class Meta:
        model = Borrowing
        fields = ('id', 'borrow_date', 'expected_return_date', 'actual_return_date', 'book', 'user')
        read_only_fields = fields
