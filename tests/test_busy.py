from cyclecontrol.busy import WorkWatcher
from cyclecontrol.sessions import health_of


def table(*rows):
    return lambda: list(rows)


def test_a_gates_run_under_the_builder_counts_as_busy():
    rows = [
        (10, 1, "claude --agent spp-builder"),
        (11, 10, "/bin/zsh -c gates.sh full"),
        (12, 11, "just lint"),
    ]
    assert WorkWatcher(table(*rows)).running(10) is True


def test_idle_mcp_servers_do_not_count_as_busy():
    rows = [
        (10, 1, "claude"),
        (11, 10, "npm exec chrome-devtools-mcp@latest"),
        (12, 11, "node server.js"),
    ]
    assert WorkWatcher(table(*rows)).running(10) is False


def test_work_under_another_builder_is_ignored():
    rows = [(10, 1, "claude"), (20, 1, "claude"), (21, 20, "just test-tia")]
    assert WorkWatcher(table(*rows)).running(10) is False


def test_the_answer_is_cached_for_a_few_seconds():
    now = [0.0]
    calls = []

    def counting():
        calls.append(1)
        return [(10, 1, "claude"), (11, 10, "just lint")]

    watcher = WorkWatcher(counting, lambda: now[0])
    watcher.running(10)
    watcher.running(10)
    now[0] = 6.0
    watcher.running(10)
    assert len(calls) == 2


def test_an_idle_builder_with_work_running_still_reads_as_working():
    assert health_of(True, "gates", 300, 20, busy=True) == "working"
    assert health_of(True, "gates", 300, 20, busy=False) == "waiting"
    assert health_of(True, "human", 300, 20, busy=True) == "waiting"
