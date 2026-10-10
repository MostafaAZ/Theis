from django.urls import include, path

urlpatterns = [
    path("", include("ims.urls")),
]
