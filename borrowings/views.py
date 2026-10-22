from django.db import transaction
from django.db.models import F, Prefetch
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view, inline_serializer,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from books.models import Book
from borrowings.models import Borrowing
from borrowings.serializers import BorrowingCreateSerializer, BorrowingSerializer
from notifications.services import notify_borrowing_created
from payments.models import Payment
from payments.services import (
    STRIPE_MIN_AMOUNT,
    calculate_fine_amount,
    calculate_overdue_days,
    convert_to_stripe_amount,
    create_checkout_session,
    create_fine_checkout_session,
    create_fine_payment,
    create_payment_for_borrowing,
)


@extend_schema_view(
    list=extend_schema(
        summary='List visible borrowings',
        description=(
            'Authentication is required. Regular users see only their own borrowings; '
            'staff can see all borrowings. These filters apply only to this list endpoint.'
        ),
        parameters=[
            OpenApiParameter(
                'is_active', OpenApiTypes.BOOL, OpenApiParameter.QUERY,
                description=(
                    "Accepts 'true' (not returned) or 'false' (returned), case-insensitively. "
                    'Absent or empty means no active-status filter. Any other nonempty '
                    'value returns HTTP 400; whitespace is not trimmed.'
                ),
            ),
            OpenApiParameter(
                'user_id', OpenApiTypes.INT64, OpenApiParameter.QUERY,
                description=(
                    'Staff only: filter by a positive user ID from 1 to 9223372036854775807. '
                    'Use ASCII digits; leading zeroes are accepted, signs and whitespace are not. '
                    'Absent or empty means no user filter; invalid nonempty values return HTTP 400. '
                    'An unknown valid ID returns an empty list. Ignored for regular users, '
                    'who always see only their own borrowings.'
                ),
            ),
        ],
    ),
)
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

    def _build_checkout_urls(self):
        cancel_url = self.request.build_absolute_uri(reverse('payment-cancel'))
        success_base_url = self.request.build_absolute_uri(reverse('payment-success'))
        success_url = success_base_url + '?session_id={CHECKOUT_SESSION_ID}'
        return success_url, cancel_url

    def perform_create(self, serializer):
        with transaction.atomic():
            borrowing = serializer.save(user=self.request.user)
            success_url, cancel_url = self._build_checkout_urls()
            session = create_checkout_session(
                borrowing, success_url=success_url, cancel_url=cancel_url,
            )
            create_payment_for_borrowing(borrowing, session)
            transaction.on_commit(lambda: notify_borrowing_created(borrowing))

    @extend_schema(
        summary='Return a borrowed book',
        description=(
            'Authentication is required. Regular users may return only their own borrowings; '
            'staff may return any borrowing. The server sets actual_return_date to today '
            'and restores one inventory unit. A late return creates a FINE payment, with a '
            'Stripe Checkout Session when the amount meets the USD 0.50 minimum. '
            'List filters do not apply here.'
        ),
        request=None,
        responses={
            200: BorrowingSerializer,
            401: OpenApiResponse(description='Authentication credentials missing or invalid.'),
            404: OpenApiResponse(description='Borrowing does not exist or is not visible to this user.'),
            409: OpenApiResponse(
                response=inline_serializer(
                    name='BorrowingReturnError',
                    fields={'detail': serializers.CharField()},
                ),
                description='This borrowing has already been returned.',
            ),
        },
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='return',
        url_name='return',
    )
    def return_borrowing(self, request, pk=None):
        borrowing = self.get_object()
        today = timezone.localdate()
        fine_amount = None
        if calculate_overdue_days(borrowing, today) > 0:
            fine_amount = calculate_fine_amount(borrowing, today)

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

            if fine_amount is not None:
                session = None
                if convert_to_stripe_amount(fine_amount) >= STRIPE_MIN_AMOUNT:
                    success_url, cancel_url = self._build_checkout_urls()
                    session = create_fine_checkout_session(
                        borrowing, fine_amount,
                        success_url=success_url, cancel_url=cancel_url,
                    )
                create_fine_payment(borrowing, fine_amount, session)

        borrowing = self.get_queryset().get(pk=borrowing.pk)
        serializer = self.get_serializer(borrowing)
        return Response(serializer.data, status=status.HTTP_200_OK)
