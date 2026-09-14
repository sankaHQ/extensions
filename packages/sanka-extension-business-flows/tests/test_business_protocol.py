# SPDX-License-Identifier: Apache-2.0
"""Exercise the real generator process and its bounded JSON transport."""

import subprocess
import sys
from pathlib import Path

from test_business_flows import request

from sanka_extensions.flow import BlueprintResponse, decode_message, encode_message


def run(payload, *arguments):
    return subprocess.run(
        [sys.executable, "-I", "-m", "sanka_extension_business_flows", *arguments],
        input=payload,
        capture_output=True,
        cwd=Path.home(),
        timeout=10,
    )


def test_generator_process_round_trip():
    selected = request()
    result = run(encode_message(selected))
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    response = BlueprintResponse.from_dict(decode_message(result.stdout))
    response.validate_for(selected)
    assert response.outcome == "success"


def test_well_formed_unsupported_request_has_one_correlated_error():
    selected = request(capabilities=())
    result = run(encode_message(selected))
    assert result.returncode == 0
    response = BlueprintResponse.from_dict(decode_message(result.stdout))
    response.validate_for(selected)
    assert response.outcome == "error"
    assert response.error.code == "BUSINESS_RECIPE_UNSUPPORTED"


def test_malformed_oversized_or_extra_command_cannot_execute_or_echo_input():
    for payload in (b'{"secret":"DO_NOT_ECHO","secret":"duplicate"}', b"x" * (4 * 1024 * 1024 + 1)):
        result = run(payload)
        assert result.returncode == 2
        assert not result.stdout
        assert b"DO_NOT_ECHO" not in result.stderr
    result = run(encode_message(request()), "activate")
    assert result.returncode == 2 and not result.stdout
