"""Validate the added MQTT translations."""

import json
import re
from pathlib import Path

import pytest

BASE = Path(__file__).parents[1] / "custom_components/aguaiot"


def flatten(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, dict):
            result.update(flatten(item, f"{path}."))
        else:
            result[path] = item
    return result


@pytest.mark.parametrize("language", ["en", "de", "fr", "nl", "pt"])
def test_translation_keys_and_placeholders(language):
    reference = json.loads((BASE / "translations/en.json").read_text())
    expected = flatten(reference["options"]["step"]["mqtt_settings"])
    actual = flatten(
        json.loads((BASE / f"translations/{language}.json").read_text())["options"][
            "step"
        ]["mqtt_settings"]
    )
    assert actual.keys() == expected.keys()
    for key, text in actual.items():
        assert isinstance(text, str) and text.strip(), key
        assert set(re.findall(r"\{[^{}]+\}", text)) == set(
            re.findall(r"\{[^{}]+\}", expected[key])
        ), key
