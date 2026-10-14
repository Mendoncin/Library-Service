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
            if self.request.user.is_staff:
                raw_user_id = self.request.query_params.get('user_id', '')
                if raw_user_id:
                    digits = raw_user_id.lstrip('0')
                    if (
                        not raw_user_id.isascii()
                        or not raw_user_id.isdigit()
                        or len(digits) > 19
                        or not 0 < int(digits or '0') <= 2**63 - 1
                    ):
                        raise ValidationError({
                            'user_id': ['Expected a positive integer user id within the 64-bit range.'],
                        })
                    queryset = queryset.filter(user_id=int(digits))

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
