"""Textual TUI for the standalone 1541 drive emulator."""
from __future__ import annotations

import collections
import os
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

if TYPE_CHECKING:
    from .c1541_emulator import Drive1541


def _escape_markup(text: str) -> str:
    """Escape square brackets in text for Textual markup safety."""
    return text.replace("[", "[[").replace("]", "]]")


# ---------------------------------------------------------------------------
# ASCII art — 38 chars wide interior so the outer box lines up at 40 cols.
# Slot shows [====================] when disk present, [                    ] empty.
# ---------------------------------------------------------------------------
_ART_DISK = (
    "+--------------------------------------+\n"
    "|  COMMODORE  1541    [{led}]    ~~~~      |\n"
    "|                                      |\n"
    "|  +--------------------------------+  |\n"
    "|  |  DRIVE {dev:<2}                      |  |\n"
    "|  |                                |  |\n"
    "|  |[==========================]    |  |\n"
    "|  +--------------------------------+  |\n"
    "+--------------------------------------+"
)
# Slot lines replace row 6 at render time (both must be exactly 40 chars).
_SLOT_LINE_FULL  = "|  |[==========================]    |  |"
_SLOT_LINE_EMPTY = "|  |[                          ]    |  |"

_LEVEL_STYLE = {
    "DEBUG":    "dim white",
    "INFO":     "green",
    "WARNING":  "yellow",
    "ERROR":    "bold red",
    "CRITICAL": "bold red",
}


# ---------------------------------------------------------------------------
# Confirmation modal
# ---------------------------------------------------------------------------
class ConfirmScreen(ModalScreen):
    """Simple yes/no confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        background: $surface;
        border: solid $primary;
        padding: 1 3;
        width: 50;
        height: auto;
    }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    Button { margin: 0 1; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No",  id="no",  variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ---------------------------------------------------------------------------
# File-picker modal (simple path input — no external deps)
# ---------------------------------------------------------------------------
class FilePickScreen(ModalScreen):
    """Minimal file path input dialog."""

    DEFAULT_CSS = """
    FilePickScreen { align: center middle; }
    #dialog {
        background: $surface;
        border: solid $primary;
        padding: 1 3;
        width: 60;
        height: auto;
    }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    Button { margin: 0 1; }
    Input { margin-top: 1; }
    """

    def __init__(self, prompt: str, initial: str = "",
                 placeholder: str = "path/to/disk.d64") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt)
            yield Input(value=self._initial, id="path-input",
                        placeholder=self._placeholder)
            with Horizontal(id="buttons"):
                yield Button("Open",   id="ok",     variant="primary")
                yield Button("Cancel", id="cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            path = self.query_one("#path-input", Input).value.strip()
            self.dismiss(path if path else None)
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        path = self.query_one("#path-input", Input).value.strip()
        self.dismiss(path if path else None)


# ---------------------------------------------------------------------------
# Drive display widget
# ---------------------------------------------------------------------------
class DriveDisplay(Static):
    led_on: reactive[bool] = reactive(False)
    disk_label: reactive[str] = reactive("(no disk)")
    status: reactive[str] = reactive("00, OK,00,00")
    device: reactive[int] = reactive(8)
    has_disk: reactive[bool] = reactive(False)

    def __init__(self, initial_device: int = 8, **kwargs) -> None:
        super().__init__(**kwargs)
        self.device = initial_device

    def render(self) -> str:
        led      = "*" if self.led_on else " "
        slot_row = _SLOT_LINE_FULL if self.has_disk else _SLOT_LINE_EMPTY
        lines = _ART_DISK.format(led=led, dev=self.device).splitlines()
        lines[6] = slot_row
        art = "\n".join(lines)
        disk_info = self.disk_label if self.disk_label else "(no disk)"
        return f"{art}\n DISK:   {disk_info}\n STATUS: {self.status}"


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class DriveTextualApp(App):
    CSS = """
    Screen { background: $surface; }
    #left  { width: 44; min-width: 44; }
    DriveDisplay { height: auto; padding: 1 2; }
    #log-panel { border: solid $primary; padding: 0 1; }
    """
    BINDINGS = [
        ("ctrl+c", "quit",        "Quit"),
        ("q",      "quit",        "Quit"),
        ("u",      "unload_disk", "Unload disk"),
        ("r",      "replace_disk","Replace disk"),
        ("n",      "new_disk",    "New blank disk"),
    ]

    def __init__(self, drive: "Drive1541", device: int, port: int,
                 log_ring: "collections.deque | None" = None) -> None:
        super().__init__()
        self._drive = drive
        self._device = device
        self._port = port
        self._log_ring: collections.deque = log_ring if log_ring is not None \
            else collections.deque(maxlen=200)
        self._ring_cursor = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield DriveDisplay(initial_device=self._device, id="disp")
            yield RichLog(id="log-panel", highlight=True, markup=True,
                          wrap=True, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"c1541 drive {self._device} — port {self._port}"
        self.set_interval(1 / 30, self._poll_drive)
        self.set_interval(1 / 10, self._poll_log)

    # ------------------------------------------------------------------
    # Drive + log polling
    # ------------------------------------------------------------------

    def _poll_drive(self) -> None:
        d = self._drive
        try:
            disp: DriveDisplay = self.query_one("#disp", DriveDisplay)
        except Exception:
            return
        disp.led_on    = bool(getattr(d, "led_on", False))
        disk_fn        = getattr(d, "disk_filename", None) or ""
        has_disk       = getattr(d, "disk", None) is not None
        disp.has_disk  = has_disk
        if not has_disk:
            disp.disk_label = "(no disk)"
        elif disk_fn:
            disp.disk_label = os.path.basename(disk_fn)
        else:
            disp.disk_label = "(blank)"
        helper = getattr(d, "_disk_helper", None)
        if helper is not None:
            try:
                disp.status = helper.get_status()
            except Exception:
                pass

    def _poll_log(self) -> None:
        ring = self._log_ring
        total = len(ring)
        if self._ring_cursor >= total:
            return
        try:
            log_widget: RichLog = self.query_one("#log-panel", RichLog)
        except Exception:
            return
        new_entries = list(ring)[self._ring_cursor:]
        self._ring_cursor = total
        for ts, level, message in new_entries:
            style = _LEVEL_STYLE.get(level, "white")
            log_widget.write(
                f"[dim]{ts}[/dim] [{style}]{level:<7}[/{style}] {message}"
            )

    # ------------------------------------------------------------------
    # Key actions
    # ------------------------------------------------------------------

    def action_unload_disk(self) -> None:
        disk_fn = getattr(self._drive, "disk_filename", None)
        if not disk_fn:
            self._log_ring.append(("--:--:--", "WARNING", "No disk to unload"))
            return
        label = os.path.basename(disk_fn)
        def _confirmed(yes: bool) -> None:
            if yes:
                self._drive.detach_disk()
                self._log_ring.append(("--:--:--", "INFO",
                                       f"Disk unloaded: {_escape_markup(label)}"))
        self.push_screen(ConfirmScreen(f"Unload  '{label}'?"), _confirmed)

    def action_replace_disk(self) -> None:
        current = getattr(self._drive, "disk_filename", None) or ""
        def _picked(path: str | None) -> None:
            if not path:
                return
            if not os.path.isfile(path):
                self._log_ring.append(("--:--:--", "ERROR",
                                       f"File not found: {_escape_markup(path)}"))
                return
            try:
                from ..d64 import load_d64
                disk = load_d64(path)
                self._drive.attach_disk(disk, path)
                name, did = disk.read_bam()
                self._log_ring.append(("--:--:--", "INFO",
                    f"Disk replaced — '{name.strip()}' (id: {did.strip()}) — {path}"))
            except Exception as exc:
                self._log_ring.append(("--:--:--", "ERROR",
                                       f"Failed to load disk: {exc}"))
        self.push_screen(FilePickScreen("Select D64 image:", initial=current),
                         _picked)

    def action_new_disk(self) -> None:
        def _got_path(path: str | None) -> None:
            if not path:
                return
            if not path.endswith(".d64"):
                path = path + ".d64"
            def _confirmed(yes: bool) -> None:
                if not yes:
                    return
                try:
                    from ..d64 import create_blank_d64
                    import os as _os
                    disk_name = _os.path.splitext(_os.path.basename(path))[0][:16].upper()
                    disk = create_blank_d64(disk_name=disk_name, disk_id="00")
                    disk.save_to_file(path)
                    self._drive.attach_disk(disk, path)
                    self._log_ring.append(("--:--:--", "INFO",
                                           f"Blank disk created and inserted: {_escape_markup(path)}"))
                except Exception as exc:
                    self._log_ring.append(("--:--:--", "ERROR",
                                           f"Failed to create blank disk: {exc}"))
            self.push_screen(
                ConfirmScreen(f"Create new blank disk at:\n'{path}'?"),
                _confirmed,
            )
        self.push_screen(
            FilePickScreen("New blank disk — save as:", placeholder="new_disk.d64"),
            _got_path,
        )
