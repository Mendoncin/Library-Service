from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import serializers

from books.models import Book
from books.serializers import BookSerializer
from borrowings.models import Borrowing


class BorrowingSerializer(serializers.ModelSerializer):
    book = BookSerializer(read_only=True)

    class Meta:
        model = Borrowing
        fields = ('id', 'borrow_date', 'expected_return_date', 'actual_return_date', 'book', 'user')
        read_only_fields = fields


class BorrowingCreateSerializer(serializers.ModelSerializer):
    out_of_stock_message = 'This book is out of stock.'
    invalid_return_date_message = 'Expected return date cannot be before today.'

    class Meta:
        model = Borrowing
        fields = ('id', 'book', 'expected_return_date')
        read_only_fields = ('id',)

    def validate_book(self, book):
        if book.inventory <= 0:
            raise serializers.ValidationError(self.out_of_stock_message)
        return book

    def validate_expected_return_date(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError(self.invalid_return_date_message)
        return value

    def create(self, validated_data):
        borrow_date = timezone.localdate()
        if validated_data['expected_return_date'] < borrow_date:
            raise serializers.ValidationError({
                'expected_return_date': self.invalid_return_date_message,
            })

        with transaction.atomic():
            updated = Book.objects.filter(
                pk=validated_data['book'].pk,
                inventory__gt=0,
            ).update(inventory=F('inventory') - 1)
            if updated == 0:
                raise serializers.ValidationError({'book': self.out_of_stock_message})
            return Borrowing.objects.create(borrow_date=borrow_date, **validated_data)
