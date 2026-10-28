"""camera settings step definitions"""

from urllib.parse import urlparse

import requests
from behave import given, then, when
from behave.runner import Context
from hamcrest import assert_that, equal_to

from blackvuesync_v2.server import camera_config, camera_schema

# timeout in seconds for camera requests
_TIMEOUT = 10.0


def _camera_address(context: Context) -> str:
    """returns the mock dashcam address as reachable from the test runner."""
    address: str = urlparse(context.mock_dashcam_url).netloc
    return address


def _served_config(context: Context) -> bytes:
    """reads the config.ini the mock dashcam currently holds."""
    response = requests.get(
        f"{context.mock_dashcam_url}/Config/config.ini", timeout=_TIMEOUT
    )
    response.raise_for_status()
    content: bytes = response.content
    return content


@given('the dashcam config has "{key}" set to "{value}"')
def dashcam_config(context: Context, key: str, value: str) -> None:
    """seeds the mock dashcam with an invented config.ini."""
    content = f"[Tab1]\nTimeZone=1000\n[Tab3]\n{key}={value}\nAlertLimit=115\n"
    response = requests.post(
        f"{context.mock_dashcam_url}/mock/config",
        data=content.encode(),
        timeout=_TIMEOUT,
    )
    response.raise_for_status()


@when('the camera setting "{name}" is applied as "{value}"')
def apply_camera_setting(context: Context, name: str, value: str) -> None:
    """patches, self-checks and uploads one setting through the camera client."""
    address = _camera_address(context)
    read = camera_config.fetch(address, _TIMEOUT)
    assert read is not None, "the mock dashcam served no config.ini"
    before = _served_config(context)

    changes, errors = camera_schema.prepare_changes({name: value})
    assert not errors, errors

    after = camera_config.patch(before, changes)
    camera_config.self_check(before, after, changes)
    camera_config.upload(address, after, _TIMEOUT)

    context.config_before = before


@then('the dashcam config has "{key}" set to "{value}"')
def assert_dashcam_config(context: Context, key: str, value: str) -> None:
    """verifies the value of a key in the config.ini the mock dashcam holds."""
    config = camera_config.parse(_served_config(context))
    values = {entry.key: entry.value for entry in config.entries}
    assert_that(values[key], equal_to(value))


@then('the dashcam config differs only in "{key}"')
def assert_dashcam_config_differs_only(context: Context, key: str) -> None:
    """verifies that the upload changed no key other than the given one."""
    changes = camera_config.diff(
        camera_config.parse(context.config_before),
        camera_config.parse(_served_config(context)),
    )
    assert_that([change.key for change in changes], equal_to([key]))
