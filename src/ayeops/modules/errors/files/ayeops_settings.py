import importlib

globals().update(
    {
        name: value
        for name, value in vars(importlib.import_module("glitchtip.settings")).items()
        if name.isupper()
    }
)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
