from avarch.serialization import canonical_json


def test_canonical_json_sorts_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_uses_compact_separators() -> None:
    assert canonical_json({"items": [1, 2], "ok": True}) == '{"items":[1,2],"ok":true}'


def test_canonical_json_preserves_unicode() -> None:
    assert canonical_json({"title": "Amélie"}) == '{"title":"Amélie"}'
