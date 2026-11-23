from cyclecontrol.circleci import Failure, workflow_id


def test_the_workflow_id_comes_out_of_a_check_link():
    link = "https://app.circleci.com/workflow/4fbcc5b8-bc1a-4847-a068-298cb0e03b4c/job/e4da571c-0c6b-4bc1-ba99-29afe90bdc32"
    assert workflow_id(link) == "4fbcc5b8-bc1a-4847-a068-298cb0e03b4c"
    assert workflow_id("https://app.aviator.co/queue") == ""


def test_a_shallow_clone_race_in_the_fetch_step_is_infrastructure_and_names_its_fatal_line():
    failure = Failure(
        "wf",
        "lint-js-style",
        "Fetching merge base with develop",
        "Resolving deltas: 100%\nFrom github.com:AgentSoftware/street\nfatal: shallow file has changed since we read it\n\nExited with code exit status 128",
    )
    assert failure.infra
    assert failure.line == "fatal: shallow file has changed since we read it"


def test_a_failing_test_is_not_infrastructure():
    failure = Failure(
        "wf",
        "test-jest",
        "Run jest",
        "FAIL src/a.test.ts\n  expected 1 to be 2\nExited with code exit status 1",
    )
    assert not failure.infra and failure.line == "expected 1 to be 2"
