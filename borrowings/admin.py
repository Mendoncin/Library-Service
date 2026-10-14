from django.contrib import admin

from borrowings.models import Borrowing


@admin.register(Borrowing)
class BorrowingAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user', 'book', 'borrow_date',
        'expected_return_date', 'actual_return_date',
    )
    search_fields = ('user__email', 'book__title', 'book__author')
    list_filter = ('borrow_date', 'expected_return_date', 'actual_return_date')
    list_select_related = ('user', 'book')
    readonly_fields = (
        'id', 'user', 'book', 'borrow_date',
        'expected_return_date', 'actual_return_date',
    )

    # Borrowings are created through the API's transactional flow only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
