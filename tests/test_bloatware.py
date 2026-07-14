import pytest

import bloatware
from actions import ProtectedAppError, debloat, undo
from tests.test_actions import FakeAdb, log  # reuse fixtures


@pytest.fixture(autouse=True)
def _tmp_data_dir(monkeypatch, tmp_path):
    import adb
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)


def test_seed_excludes_carrier_config_adjacent_omcagent():
    assert "com.samsung.android.app.omcagent" not in bloatware.BLOAT_SEED


def test_find_bloat_matches_only_listed_system_packages(monkeypatch, tmp_path):
    class SysAdb(FakeAdb):
        def shell_text(self, args, timeout=10):
            if args == ["pm", "list", "packages", "-s"]:
                return ("package:com.facebook.appmanager\n"
                        "package:com.android.systemui\n")
            return super().shell_text(args, timeout)
    assert bloatware.find_bloat(SysAdb()) == ["com.facebook.appmanager"]


def test_user_bloat_file_extends_seed(tmp_path):
    (tmp_path / "bloatware.txt").write_text("com.carrier.junk  # verified\n",
                                            encoding="utf-8")
    class SysAdb(FakeAdb):
        def shell_text(self, args, timeout=10):
            if args == ["pm", "list", "packages", "-s"]:
                return "package:com.carrier.junk\n"
            return super().shell_text(args, timeout)
    assert bloatware.find_bloat(SysAdb()) == ["com.carrier.junk"]


def test_debloat_disables_and_refuses_unlisted(log):
    adb = FakeAdb()
    assert debloat(adb, "com.facebook.appmanager", log)
    assert "com.facebook.appmanager" in adb.disabled
    entry = log.recent()[0]
    assert entry["action"] == "debloat"
    undo(adb, entry, log)
    assert "com.facebook.appmanager" not in adb.disabled
    with pytest.raises(ProtectedAppError):
        debloat(adb, "com.android.systemui", log)   # not on the list -> refused
