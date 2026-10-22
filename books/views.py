from django.db.models.deletion import ProtectedError
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.response import Response

from books.models import Book
from books.permissions import IsAdminOrReadOnly
from books.serializers import BookSerializer


@extend_schema_view(
    list=extend_schema(auth=[], description='Public book catalogue.'),
    retrieve=extend_schema(auth=[], description='Public book details.'),
    create=extend_schema(description='Create a book. Staff authentication is required.'),
    update=extend_schema(description='Update a book. Staff authentication is required.'),
    partial_update=extend_schema(description='Partially update a book. Staff authentication is required.'),
    destroy=extend_schema(
        description='Delete a book. Staff authentication is required.',
        responses={
            204: OpenApiResponse(description='Book deleted.'),
            409: OpenApiResponse(description='Book has related borrowings and cannot be deleted.'),
        },
    ),
)
class BookViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminOrReadOnly]
    queryset = Book.objects.all()
    serializer_class = BookSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError:
            return Response(
                {'detail': 'This book cannot be deleted because it has related borrowings.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
