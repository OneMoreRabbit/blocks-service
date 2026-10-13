"""Versioned URI corpora, independent of HTTP and database fixtures."""

import json
import pathlib

import pytest

from blocks_service.project_items import InvalidItem, validate_uri


CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_3.json").read_text()
)
CORPUS_V04 = json.loads(
    (pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_4.json").read_text()
)
CORPUS_V05 = json.loads(
    (pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_5.json").read_text()
)


@pytest.mark.parametrize("uri", CORPUS["accepted"])
def test_shared_v03_accepted_uri(uri):
    assert validate_uri(uri, api_version="0.7") == uri


@pytest.mark.parametrize("uri", CORPUS["refused"])
def test_shared_v03_refused_uri(uri):
    with pytest.raises(InvalidItem):
        validate_uri(uri, api_version="0.7")


@pytest.mark.parametrize("uri", CORPUS_V04["accepted"])
def test_shared_v04_accepted_uri(uri):
    assert validate_uri(uri, api_version="0.8") == uri


@pytest.mark.parametrize("uri", CORPUS_V04["refused"])
def test_shared_v04_refused_uri(uri):
    with pytest.raises(InvalidItem):
        validate_uri(uri, api_version="0.8")


@pytest.mark.parametrize("uri", CORPUS_V05["accepted"])
def test_shared_v05_accepted_uri(uri):
    assert validate_uri(uri, api_version="0.8") == uri


@pytest.mark.parametrize("uri", CORPUS_V05["refused"])
def test_shared_v05_refused_uri(uri):
    with pytest.raises(InvalidItem):
        validate_uri(uri, api_version="0.8")


def test_v05_replaces_only_the_private_positive_case_and_covers_repeated_keys():
    assert CORPUS_V05["accepted"][:-2] == CORPUS_V04["accepted"][:-1]
    assert CORPUS_V05["refused"] == CORPUS_V04["refused"]
    assert CORPUS_V04["accepted"][-1] not in CORPUS_V05["accepted"]
    synthetic, repeated = CORPUS_V05["accepted"][-2:]
    assert synthetic.startswith("https://onedrive.live.com/edit.aspx?")
    assert "%21" in synthetic and "%28" in synthetic and "%7C" in synthetic
    assert "%2F" in synthetic and "%29" in synthetic
    assert "{" in synthetic and "}" in synthetic and synthetic.endswith("&end")
    assert repeated.count("&id=") == 2
    assert repeated.endswith("&id=synthetic-copy&end")
