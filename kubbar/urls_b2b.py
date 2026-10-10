from django.urls import include, path

urlpatterns = [
    path("", include("b2b.urls")),
]
