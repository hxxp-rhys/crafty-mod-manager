"""UI-layer tests (offscreen Qt).

These cover the parts that unit tests of the manager can't reach: background
task delivery, the busy-flag re-entrancy guard, and the widget wiring that
turns a task result into what the user sees.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from craftymm.ui import workers  # noqa: E402
from craftymm.ui.workers import defer, run_task  # noqa: E402
from fake_crafty import SERVER_ID, TOKEN, FakeCrafty  # noqa: E402
from test_all import make_jar  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def pump(ms: int = 400) -> None:
    """Spin the event loop for a while so queued signals get delivered."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# --------------------------------------------------------------------------- #
#  Background tasks
# --------------------------------------------------------------------------- #
def test_task_callbacks_are_delivered_when_the_caller_drops_the_handle(qapp):
    """Regression: the QRunnable used to auto-delete itself (taking its
    TaskSignals with it) before the GUI thread ran the callbacks, so nothing
    ever happened after a background job."""
    seen = []
    for i in range(25):
        run_task(
            lambda n=i: n * 2,
            on_done=lambda v: seen.append(("done", v)),
            on_finished=lambda: seen.append(("finished", None)),
        )
    pump(900)
    dones = [v for kind, v in seen if kind == "done"]
    assert len(dones) == 25, f"only {len(dones)}/25 results were delivered"
    assert sorted(dones) == [i * 2 for i in range(25)]
    assert sum(1 for kind, _ in seen if kind == "finished") == 25
    assert workers.active_count() == 0, "tasks were not released"


def test_task_failure_reports_the_message(qapp):
    got = []

    def boom():
        raise ValueError("kaboom")

    run_task(boom, on_error=lambda msg, tb: got.append((msg, tb)))
    pump(500)
    assert got and got[0][0] == "kaboom"
    assert "ValueError" in got[0][1]


def test_progress_callback_is_injected(qapp):
    ticks = []

    def work(progress=None):
        for i in range(1, 4):
            progress(f"step {i}", i, 3)
        return "ok"

    run_task(work, on_progress=lambda m, c, t: ticks.append((m, c, t)))
    pump(500)
    assert ticks == [("step 1", 1, 3), ("step 2", 2, 3), ("step 3", 3, 3)]


def test_done_fires_before_finished(qapp):
    order = []
    run_task(
        lambda: 1,
        on_done=lambda _: order.append("done"),
        on_finished=lambda: order.append("finished"),
    )
    pump(400)
    assert order == ["done", "finished"]


def test_defer_runs_after_the_already_queued_finished(qapp):
    """This ordering is what makes chained refreshes work despite the busy
    guard, so pin it down."""
    order = []
    run_task(
        lambda: 1,
        on_done=lambda _: (order.append("done"), defer(lambda: order.append("deferred"))),
        on_finished=lambda: order.append("finished"),
    )
    pump(500)
    assert order == ["done", "finished", "deferred"]


# --------------------------------------------------------------------------- #
#  Tabs wired to a live (fake) server
# --------------------------------------------------------------------------- #
@pytest.fixture
def wired(qapp, tmp_path, monkeypatch):
    """A ModsTab + FilesTab bound to a manager talking to a fake Crafty."""
    import craftymm.config as cfg

    appdir = tmp_path / "appdata"
    appdir.mkdir()
    monkeypatch.setattr(cfg, "app_dir", lambda: appdir)
    monkeypatch.setattr(cfg, "KEYRING_OK", False)
    monkeypatch.setattr(cfg, "_FALLBACK_PATH", appdir / "secrets.json")

    root = tmp_path / "server"
    (root / "mods").mkdir(parents=True)
    for name, mid in (("alpha-1.0.0.jar", "alpha"), ("beta-2.0.0.jar", "beta")):
        (root / "mods" / name).write_bytes(
            make_jar({"fabric.mod.json": json.dumps(
                {"id": mid, "name": mid.title(), "version": "1.0.0"})})
        )
    (root / "server.properties").write_text("max-players=20\n", encoding="utf-8")

    with FakeCrafty(root) as fake:
        from craftymm.backends.crafty import CraftyBackend
        from craftymm.config import Profile, Settings
        from craftymm.manager import ModManager
        from craftymm.ui.files_tab import FilesTab
        from craftymm.ui.mods_tab import ModsTab

        backend = CraftyBackend(fake.url, token=TOKEN)
        backend.connect()
        backend.select_server(SERVER_ID)
        profile = Profile(id="ui-test", name="UI", mods_dir="mods")
        manager = ModManager(backend, profile, Settings(), {})

        mods_tab = ModsTab()
        files_tab = FilesTab()
        mods_tab.set_manager(manager)
        files_tab.set_manager(manager)
        pump(400)
        yield mods_tab, files_tab, manager, root


def test_mods_tab_refresh_populates_the_table(wired, monkeypatch):
    mods_tab, _, _, _ = wired
    mods_tab.refresh()
    pump(1200)
    assert mods_tab.table.rowCount() == 2
    names = {mods_tab.table.item(r, 0).text() for r in range(2)}
    assert names == {"Alpha", "Beta"}
    assert mods_tab._busy is False


def test_delete_refreshes_the_table_afterwards(wired, monkeypatch):
    """Regression: the follow-up refresh was issued while the busy flag was
    still set, so it silently no-opped and the table kept showing dead rows."""
    from PySide6.QtWidgets import QMessageBox

    mods_tab, _, _, root = wired
    mods_tab.refresh()
    pump(1200)
    assert mods_tab.table.rowCount() == 2

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    mods_tab.table.selectRow(0)
    mods_tab.delete_selected()
    pump(1500)

    assert len(list((root / "mods").iterdir())) == 1
    assert mods_tab.table.rowCount() == 1, "table still shows the deleted mod"


def test_failed_task_still_clears_busy(wired):
    from PySide6.QtWidgets import QMessageBox

    mods_tab, _, manager, _ = wired
    manager.profile.mods_dir = "no-such-folder"
    QMessageBox.critical = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
    mods_tab.refresh()
    pump(1200)
    assert mods_tab._busy is False
    assert mods_tab.refresh_btn.isEnabled()


def test_files_tab_open_and_save(wired):
    _, files_tab, _, root = wired
    files_tab.navigate("")
    pump(900)
    entry = next(e for e in files_tab.entries if e.name == "server.properties")
    files_tab.open_file(entry)
    pump(900)
    assert "max-players=20" in files_tab.editor_pane.editor.toPlainText()
    assert files_tab.editor_pane.editor.is_dirty is False

    files_tab.editor_pane.editor.setPlainText("max-players=40\n")
    assert files_tab.editor_pane.editor.is_dirty is True
    assert files_tab.save_btn.isEnabled()
    files_tab.save()
    pump(1200)
    assert (root / "server.properties").read_text() == "max-players=40\n"
    assert files_tab.editor_pane.editor.is_dirty is False


def test_new_folder_then_listing_refreshes(wired, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    _, files_tab, _, root = wired
    files_tab.navigate("")
    pump(900)
    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("newdir", True))
    )
    files_tab._create(directory=True)
    pump(1500)
    assert (root / "newdir").is_dir()
    assert any(e.name == "newdir" for e in files_tab.entries), "listing not refreshed"


# --------------------------------------------------------------------------- #
#  Browse tab: stale responses
# --------------------------------------------------------------------------- #
def test_stale_version_response_is_ignored(qapp):
    from craftymm.models import VersionInfo
    from craftymm.ui.browse_tab import BrowseTab

    tab = BrowseTab()
    tab._version_seq = 5

    old = [VersionInfo("modrinth", "old", "p", "Old", "0.1", "old.jar", "u")]
    new = [VersionInfo("modrinth", "new", "p", "New", "9.9", "new.jar", "u")]
    tab._show_versions(new, seq=5)
    tab._show_versions(old, seq=3)  # a slow reply for a mod the user left
    assert [v.version_id for v in tab.versions] == ["new"]


def test_stale_search_response_is_ignored(qapp):
    from craftymm.models import ProjectHit
    from craftymm.ui.browse_tab import BrowseTab

    tab = BrowseTab()
    tab._search_seq = 2
    tab._show_results([ProjectHit("modrinth", "b", "b", "Newer")], seq=2)
    tab._show_results([ProjectHit("modrinth", "a", "a", "Older")], seq=1)
    assert [h.title for h in tab.hits] == ["Newer"]


# --------------------------------------------------------------------------- #
#  Status bar
# --------------------------------------------------------------------------- #
def test_power_label_handles_a_silent_ssh_status_command(qapp, tmp_path, monkeypatch):
    """`systemctl is-active --quiet` prints nothing; splitting [0] used to
    raise IndexError and blank the toolbar."""
    import craftymm.config as cfg

    appdir = tmp_path / "cfg"
    appdir.mkdir()
    monkeypatch.setattr(cfg, "app_dir", lambda: appdir)
    monkeypatch.setattr(cfg, "CONFIG_PATH", appdir / "config.json")

    from craftymm.config import Settings
    from craftymm.ui.main_window import MainWindow

    w = MainWindow(Settings())
    w._show_power({"exit_code": 0, "output": ""})
    assert w.power_label.text() == "ok"
    w._show_power({"exit_code": 3, "output": ""})
    assert w.power_label.text() == "no output"
    w._show_power({"exit_code": 0, "output": "active\nextra"})
    assert w.power_label.text() == "active"
    w._show_power({"running": True, "online": 2, "max": 20, "cpu": 5, "mem": "1GB"})
    assert "running" in w.power_label.text() and "2/20" in w.power_label.text()
    w.close()
