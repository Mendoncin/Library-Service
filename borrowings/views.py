from django.db import transaction
from django.db.models import F, Prefetch
from django.urls import reverse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from books.models import Book
from borrowings.models import Borrowing
from borrowings.serializers import BorrowingCreateSerializer, BorrowingSerializer
from notifications.services import notify_borrowing_created
from payments.models import Payment
from payments.services import create_checkout_session, create_payment_for_borrowing


class BorrowingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Borrowing.objects.select_related('book').prefetch_related(
        Prefetch('payments', queryset=Payment.objects.order_by('id')),
    ).order_by('id')
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
        with transaction.atomic():
            borrowing = serializer.save(user=self.request.user)
            cancel_url = self.request.build_absolute_uri(
                reverse('payment-cancel'),
            )
            success_base_url = self.request.build_absolute_uri(
                reverse('payment-success'),
            )
            success_url = success_base_url + '?session_id={CHECKOUT_SESSION_ID}'
            session = create_checkout_session(
                borrowing, success_url=success_url, cancel_url=cancel_url,
            )
            create_payment_for_borrowing(borrowing, session)
            transaction.on_commit(lambda: notify_borrowing_created(borrowing))

    @action(
        detail=True,
        methods=['post'],
        url_path='return',
        url_name='return',
    )
    def return_borrowing(self, request, pk=None):
        borrowing = self.get_object()
        today = timezone.localdate()

        with transaction.atomic():
            updated = Borrowing.objects.filter(
                pk=borrowing.pk,
                actual_return_date__isnull=True,
            ).update(actual_return_date=today)
            if updated == 0:
                return Response(
                    {'detail': 'This borrowing has already been returned.'},
                    status=status.HTTP_409_CONFLICT,
                )

            Book.objects.filter(pk=borrowing.book_id).update(inventory=F('inventory') + 1)

        borrowing = self.get_queryset().get(pk=borrowing.pk)
        serializer = self.get_serializer(borrowing)
        return Response(serializer.data, status=status.HTTP_200_OK)
