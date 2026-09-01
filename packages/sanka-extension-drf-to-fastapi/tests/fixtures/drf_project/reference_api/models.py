# SPDX-License-Identifier: Apache-2.0
from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "reference_api"
