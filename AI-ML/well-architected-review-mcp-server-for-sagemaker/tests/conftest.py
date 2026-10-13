# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import pytest
from typing import Dict


TEMP_ENV_VARS: Dict[str, str] = {}


@pytest.fixture(scope='session', autouse=True)
def tests_setup_and_teardown():
    """Mock environment and module variables for testing."""
    global TEMP_ENV_VARS
    old_environ = dict(os.environ)
    os.environ.update(TEMP_ENV_VARS)

    yield

    os.environ.clear()
    os.environ.update(old_environ)
