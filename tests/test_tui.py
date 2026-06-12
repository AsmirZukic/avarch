from avarch.tui.app import AvarchTuiApp


def test_tui_app_can_be_constructed() -> None:
    app = AvarchTuiApp()

    assert app is not None
