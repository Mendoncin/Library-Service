from rest_framework import generics

from users.serializers import UserRegistrationSerializer


class UserRegistrationView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
