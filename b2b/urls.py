from django.urls import path

from . import views

app_name = "b2b"

urlpatterns = [
    path("", views.home, name="home"),
    path("lang/<str:code>/", views.set_language, name="set_language"),
    path("register/", views.register, name="register"),
    path("register/confirm/<uuid:token>/", views.confirm_email, name="confirm_email"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("unlock/<str:token>/", views.unlock, name="unlock"),
    path("password-reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", views.auth_views.PasswordResetDoneView.as_view(
        template_name="b2b/password_reset_done.html"), name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("account/", views.account, name="account"),
    path("catalogue/", views.catalogue, name="catalogue"),
    path("catalogue/<int:pk>/", views.listing_detail, name="listing"),
    path("cart/", views.cart, name="cart"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:pk>/", views.order_detail, name="order"),
    path("orders/<int:pk>/pay/", views.pay, name="pay"),
    path("orders/<int:pk>/invoice/", views.invoice_pdf, name="invoice"),
]
