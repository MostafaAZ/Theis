import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kubbar.settings.b2b")
application = get_wsgi_application()
