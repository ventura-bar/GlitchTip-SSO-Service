#!/usr/bin/env python
"""
GlitchTip bootstrap script — run via `python /glitchtip-init.py` inside the GlitchTip container.
Creates the superuser (idempotent) and the proxy admin API token with full scopes.
"""
import os
import sys

sys.path.insert(0, "/code")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

import django

django.setup()

from django.contrib.auth import get_user_model
from apps.api_tokens.models import APIToken

User = get_user_model()
email = os.environ["DJANGO_SUPERUSER_EMAIL"]
password = os.environ["DJANGO_SUPERUSER_PASSWORD"]
proxy_token = os.environ["GLITCHTIP_PROXY_TOKEN"]

# Create superuser
user, created = User.objects.get_or_create(
    email=email,
    defaults={"is_superuser": True, "is_staff": True},
)
if created:
    user.set_password(password)
    user.save()
    print(f"Created superuser: {email}")
else:
    print(f"Superuser already exists: {email}")

# Create / update proxy admin token with all scopes
all_scopes = [
    "project:read", "project:write", "project:admin", "project:releases",
    "team:read", "team:write", "team:admin",
    "event:read", "event:write", "event:admin",
    "org:read", "org:write", "org:admin",
    "member:read", "member:write", "member:admin",
]

try:
    t = APIToken.objects.get(user=user, label="proxy-admin")
    t.token = proxy_token
except APIToken.DoesNotExist:
    t = APIToken(user=user, label="proxy-admin", token=proxy_token)

for scope in all_scopes:
    setattr(t.scopes, scope, True)
t.save()
print(f"Proxy admin token ready: {t.token[:12]}...")
