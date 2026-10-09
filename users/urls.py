from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from users.views import CurrentUserView, UserRegistrationView


urlpatterns = [
    path('', UserRegistrationView.as_view(), name='user-register'),
    path('me/', CurrentUserView.as_view(), name='user-me'),
    path('token/', TokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
]
