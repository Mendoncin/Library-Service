from rest_framework.routers import SimpleRouter

from payments.views import PaymentViewSet


router = SimpleRouter()
router.register('', PaymentViewSet)

urlpatterns = router.urls
