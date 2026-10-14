from rest_framework import mixins, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from borrowings.models import Borrowing
from borrowings.serializers import BorrowingCreateSerializer, BorrowingSerializer


class BorrowingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Borrowing.objects.select_related('book').order_by('id')
    serializer_class = BorrowingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.user.is_staff:
            queryset = queryset.filter(user=self.request.user)

        if self.action == 'list':
            is_active = self.request.query_params.get('is_active', '').lower()
            if is_active in ('true', 'false'):
                queryset = queryset.filter(actual_return_date__isnull=is_active == 'true')
            elif is_active:
                raise ValidationError({'is_active': ["Expected 'true' or 'false'."]})
        return queryset

    def get_serializer_class(self):
        if self.action == 'create':
            return BorrowingCreateSerializer
        return super().get_serializer_class()

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
