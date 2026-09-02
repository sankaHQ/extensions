from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from documents.models import AccessToken


class ExpiringTokenAuthentication(TokenAuthentication):
    """Database token authentication with one extra expiry branch."""

    model = AccessToken

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if token.expires_at <= timezone.now():
            raise AuthenticationFailed("Token has expired.")
        return user, token
