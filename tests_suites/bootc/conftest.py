"""
Pytest configuration for bootc switch tests.

--bootc-switch-image is registered in the root conftest.py (tests_suites/conftest.py)
so it is available for the full-suite invocation:

    pytest tests_suites/bootc/ tests_suites/ --bootc-switch-image=<image>
"""
import pytest


@pytest.fixture(scope="session")
def bootc_new_image(request):
    """Return the target image from --bootc-switch-image, or skip."""
    image = request.config.getoption("--bootc-switch-image")
    if not image:
        pytest.skip(
            "Pass --bootc-switch-image=<image> to run bootc switch tests"
        )
    return image
