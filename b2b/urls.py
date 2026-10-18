from django.urls import path

from . import demo, shop, views

app_name = "b2b"

urlpatterns = [
    path("", shop.home, name="home"),
    path("lang/<str:code>/", views.set_language, name="set_language"),
    path("register/", views.register, name="register"),
    path("register/confirm/<uuid:token>/", views.confirm_email, name="confirm_email"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("demo/login/<int:pk>/", demo.demo_login, name="demo_login"),
    path("unlock/<str:token>/", views.unlock, name="unlock"),
    path("password-reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", views.auth_views.PasswordResetDoneView.as_view(
        template_name="b2b/password_reset_done.html"), name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("catalogue/", shop.catalogue, name="catalogue"),
    path("catalogue/<int:pk>/", shop.listing_detail, name="listing"),
    path("products/<int:pk>/image/", shop.product_image, name="product_image"),
    path("cart/", views.cart, name="cart"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:pk>/", views.order_detail, name="order"),
    path("orders/<int:pk>/pay/", views.pay, name="pay"),
    path("orders/<int:pk>/invoice/", views.invoice_pdf, name="invoice"),
    path("account/", shop.account, name="account"),
    path("account/profile/", shop.account_profile, name="account_profile"),
    path("account/documents/", shop.account_documents, name="account_documents"),
    path("account/invoices/", shop.account_invoices, name="account_invoices"),
    path("account/messages/", shop.account_messages, name="account_messages"),
    path("contact/", shop.contact, name="contact"),
    path("how-it-works/", shop.page, {"name": "how"}, name="how"),
    path("faq/", shop.page, {"name": "faq"}, name="faq"),
    path("terms/", shop.page, {"name": "terms"}, name="terms"),
    path("privacy/", shop.page, {"name": "privacy"}, name="privacy"),
]
