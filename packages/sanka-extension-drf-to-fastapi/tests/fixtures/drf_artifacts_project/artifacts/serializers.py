from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from rest_framework import serializers

from artifacts.models import Artifact

ALLOWED_EXTENSIONS = {".csv", ".json", ".txt"}
MAX_UPLOAD_BYTES = 32


class ArtifactSerializer(serializers.ModelSerializer):
    file = serializers.FileField(write_only=True)
    download_path = serializers.SerializerMethodField()

    class Meta:
        model = Artifact
        fields = [
            "id",
            "key",
            "label",
            "original_name",
            "content_type",
            "byte_size",
            "sha256",
            "download_path",
            "file",
        ]
        read_only_fields = [
            "id",
            "original_name",
            "content_type",
            "byte_size",
            "sha256",
            "download_path",
        ]

    def validate_file(self, value: Any) -> Any:
        suffix = Path(value.name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                "Only files with .csv, .json, or .txt extensions are allowed."
            )
        if value.size > MAX_UPLOAD_BYTES:
            raise serializers.ValidationError(f"File must be {MAX_UPLOAD_BYTES} bytes or smaller.")
        return value

    def create(self, validated_data: dict[str, Any]) -> Artifact:
        upload = validated_data["file"]
        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        validated_data.update(
            original_name=Path(upload.name).name,
            content_type=upload.content_type or "application/octet-stream",
            byte_size=upload.size,
            sha256=digest.hexdigest(),
        )
        return super().create(validated_data)

    def get_download_path(self, instance: Artifact) -> str:
        return f"/api/files/{instance.pk}/download/"
