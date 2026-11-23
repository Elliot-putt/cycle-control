from cyclecontrol import keepawake
from cyclecontrol.keepawake import KeepAwake


class FakeProc:
    def __init__(self):
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True


def rig(monkeypatch, enabled=True):
    started = []
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(
        keepawake.subprocess, "Popen", lambda argv, **k: started.append(argv) or FakeProc()
    )
    return KeepAwake(enabled), started


def test_it_holds_the_mac_awake_while_a_builder_runs_and_lets_go_when_none_do(monkeypatch):
    awake, started = rig(monkeypatch)
    awake.update(0)
    assert started == [] and not awake.active
    awake.update(2)
    awake.update(1)
    assert started == [["caffeinate", "-i"]] and awake.active
    awake.update(0)
    assert not awake.active
    awake.update(1)
    assert len(started) == 2


def test_it_does_nothing_when_switched_off_or_caffeinate_is_missing(monkeypatch):
    awake, started = rig(monkeypatch, enabled=False)
    awake.update(3)
    assert started == [] and not awake.active
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: None)
    KeepAwake(True).update(3)
    assert started == []
