from django.shortcuts import get_object_or_404

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from payments.models import Payment
from payments.serializers import PaymentSerializer
from payments.services import retrieve_checkout_session


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
            Payment, session_id=session_id, type=Payment.Type.PAYMENT,
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
