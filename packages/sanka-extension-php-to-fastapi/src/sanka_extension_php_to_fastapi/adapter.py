# SPDX-License-Identifier: Apache-2.0
from sanka_php_migration.lifecycle import handle as shared_handle

from sanka_extensions.code import ExtensionRequest, ExtensionResponse


def handle(request: ExtensionRequest) -> ExtensionResponse:
    return shared_handle(request, "fastapi")
