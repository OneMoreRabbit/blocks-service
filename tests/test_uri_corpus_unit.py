"""The shared v0.3 URI corpus, independent of HTTP and database fixtures."""

import json
import pathlib

import pytest

from blocks_service.project_items import InvalidItem, validate_uri


CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_3.json").read_text()
)


@pytest.mark.parametrize("uri", CORPUS["accepted"])
def test_shared_v03_accepted_uri(uri):
    assert validate_uri(uri) == uri


@pytest.mark.parametrize("uri", CORPUS["refused"])
def test_shared_v03_refused_uri(uri):
    with pytest.raises(InvalidItem):
        validate_uri(uri)
