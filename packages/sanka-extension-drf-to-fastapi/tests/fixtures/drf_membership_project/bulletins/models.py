# SPDX-License-Identifier: Apache-2.0
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class Account(AbstractUser):
    pass


class Collection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL)
    last_activity = models.DateTimeField(auto_now=True)


class Entry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    purchased = models.BooleanField()
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="entries")
