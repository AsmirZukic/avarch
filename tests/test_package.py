import avarch


def test_package_has_version() -> None:
    assert isinstance(avarch.__version__, str)
    assert avarch.__version__
