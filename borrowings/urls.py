from rest_framework.routers import SimpleRouter

from borrowings.views import BorrowingViewSet


router = SimpleRouter()
router.register('', BorrowingViewSet)

urlpatterns = router.urls
