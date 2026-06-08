from __future__ import annotations

from c64py.graphics import PygameInterface


class _DummyJoy:
    def __init__(self) -> None:
        self.axes = {0: 0.8, 1: -0.9}
        self.buttons = {0: 1, 1: 0}
        self.hats = {0: (-1, 0)}

    def get_numaxes(self) -> int:
        return 2

    def get_axis(self, axis: int) -> float:
        return float(self.axes.get(axis, 0.0))

    def get_numbuttons(self) -> int:
        return 2

    def get_button(self, button: int) -> int:
        return int(self.buttons.get(button, 0))

    def get_numhats(self) -> int:
        return 1

    def get_hat(self, hat: int):
        return self.hats.get(hat, (0, 0))


def test_gamepad_token_resolution() -> None:
    iface = PygameInterface(emulator=None)  # type: ignore[arg-type]
    j = _DummyJoy()
    assert iface._gamepad_token_active(j, "axis0+", 0.5) is True
    assert iface._gamepad_token_active(j, "axis1-", 0.5) is True
    assert iface._gamepad_token_active(j, "button0", 0.5) is True
    assert iface._gamepad_token_active(j, "button1", 0.5) is False
    assert iface._gamepad_token_active(j, "hat0:left", 0.5) is True
    assert iface._gamepad_token_active(j, "hat0:right", 0.5) is False
