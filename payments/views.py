from django.shortcuts import get_object_or_404

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer,
)
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from payments.models import Payment
from payments.serializers import PaymentSerializer
from payments.services import retrieve_checkout_session


CANCEL_MESSAGE = (
    'Payment was not completed. You can still pay later using the same '
    'checkout link while the Stripe Checkout Session remains active; '
    'sessions expire after a limited time.'
)


class PaymentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Payment.objects.order_by('id')
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.user.is_staff:
            queryset = queryset.filter(borrowing__user=self.request.user)
        return queryset

    @extend_schema(
        summary='Explain a cancelled checkout',
        description=(
            'Public, purely informational response. No request body or query parameters '
            'are required. Does not mutate the database or call/mutate Stripe. '
            'The same checkout link can be used while its Stripe Session remains active.'
        ),
        auth=[],
        request=None,
        responses={
            200: inline_serializer(
                name='PaymentCancelResponse',
                fields={'detail': serializers.CharField()},
            ),
        },
        examples=[OpenApiExample('Cancelled checkout', value={'detail': CANCEL_MESSAGE})],
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='cancel',
        url_name='cancel',
        permission_classes=[AllowAny],
    )
    def cancel(self, request):
        return Response({'detail': CANCEL_MESSAGE})

    @extend_schema(
        summary='Confirm a Stripe Checkout payment',
        description=(
            'Public redirect endpoint for PAYMENT and FINE records. Looks up the local '
            'Payment using session_id. An already PAID payment returns immediately without '
            'calling Stripe. Otherwise retrieves the Stripe Checkout Session: only '
            "payment_status='paid' changes the local status to PAID; unpaid or any other "
            'unconfirmed value leaves it PENDING. Session status and payment-status values '
            'supplied by the client are not proof of payment. Returns only detail and status.'
        ),
        auth=[],
        request=None,
        parameters=[
            OpenApiParameter(
                'session_id', OpenApiTypes.STR, OpenApiParameter.QUERY,
                required=True,
                description=(
                    'Stripe Checkout Session ID associated with a local Payment. '
                    'Surrounding whitespace is trimmed; missing, empty or whitespace-only '
                    'values return HTTP 400.'
                ),
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=inline_serializer(
                    name='PaymentSuccessResponse',
                    fields={
                        'detail': serializers.CharField(),
                        'status': serializers.ChoiceField(choices=Payment.Status.choices),
                    },
                ),
                description='Payment confirmed (PAID) or not yet confirmed (PENDING).',
                examples=[
                    OpenApiExample('Confirmed', value={'detail': 'Payment confirmed.', 'status': 'PAID'}),
                    OpenApiExample(
                        'Pending',
                        value={'detail': 'Payment has not been confirmed.', 'status': 'PENDING'},
                    ),
                ],
            ),
            400: OpenApiResponse(
                response=inline_serializer(
                    name='PaymentSessionValidationError',
                    fields={'session_id': serializers.ListField(child=serializers.CharField())},
                ),
                description='session_id is missing or blank.',
            ),
            404: OpenApiResponse(
                response=inline_serializer(
                    name='PaymentNotFoundError',
                    fields={'detail': serializers.CharField()},
                ),
                description='No local Payment matches session_id.',
            ),
        },
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='success',
        url_name='success',
        permission_classes=[AllowAny],
    )
    def success(self, request):
        session_id = request.query_params.get('session_id', '').strip()
        if not session_id:
            raise ValidationError({
                'session_id': ['This query parameter is required.'],
            })

        payment = get_object_or_404(
            Payment, session_id=session_id,
        )
        if payment.status == Payment.Status.PAID:
            return Response({
                'detail': 'Payment confirmed.',
                'status': Payment.Status.PAID,
            }, status=200)

        session = retrieve_checkout_session(session_id)
        if session.payment_status != 'paid':
            return Response({
                'detail': 'Payment has not been confirmed.',
                'status': Payment.Status.PENDING,
            }, status=200)

        Payment.objects.filter(
            pk=payment.pk, status=Payment.Status.PENDING,
        ).update(status=Payment.Status.PAID)
        return Response({
            'detail': 'Payment confirmed.',
            'status': Payment.Status.PAID,
        }, status=200)
