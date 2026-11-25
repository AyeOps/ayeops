import os

from allauth.socialaccount.models import SocialApp

SocialApp.objects.update_or_create(
    provider="google",
    defaults={
        "name": "Google",
        "client_id": os.environ["AYE_AUTH_CLIENT_ID"],
        "secret": os.environ["AYE_AUTH_CLIENT_SECRET"],
    },
)
