"""Background execution, and the things that stop it (Phase D, item 7).

A background job is the only thing in JARVIS that can go wrong for an hour
before anyone notices, so almost every test here is about a limit rather than a
capability. The one that matters most is
``test_a_job_stops_when_a_tool_needs_approval``: running unattended does not
grant authority, it removes the person who would have been asked, and the only
safe answer to that is to stop rather than assume yes.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.agents.background import (
    BackgroundRunner,
    JobRejected,
    JobState,
)
from jarvis.agents.supervisor import AgentBudget


async def _quick(job):
    """A step that finishes immediately."""
    return True, "done"


async def _forever(job):
    await asyncio.sleep(0)
    return False, "still going"


async def _settle(runner, job, *, timeout: float = 2.0) -> None:
    """Wait for a job to reach a terminal state, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if job.state.is_terminal:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job did not settle; state={job.state}")


# ── it runs, and it finishes ─────────────────────────────────────────────────


async def test_a_job_runs_and_reports_its_result() -> None:
    runner = BackgroundRunner()
    job = await runner.start(title="look something up", step=_quick)
    await _settle(runner, job)
    assert job.state is JobState.COMPLETED
    assert job.result == "done"


async def test_progress_is_visible_while_it_runs() -> None:
    """"How far along are you?" must be answerable without waiting for the end."""
    runner = BackgroundRunner()
    seen: list[int] = []

    async def counting(job):
        seen.append(job.progress.step)
        return job.progress.step >= 3, f"step {job.progress.step}"

    job = await runner.start(title="count", step=counting)
    await _settle(runner, job)
    assert seen == [1, 2, 3]
    assert job.progress.note == "step 3"


# ── every exit is terminal, and says why ─────────────────────────────────────


async def test_a_runaway_job_is_stopped_by_its_step_budget() -> None:
    runner = BackgroundRunner()
    job = await runner.start(title="loop", step=_forever, max_steps=5)
    await _settle(runner, job)
    assert job.state is JobState.FAILED
    assert "stopped after 5 steps" in job.error


async def test_a_job_that_crashes_does_not_take_the_process_with_it() -> None:
    runner = BackgroundRunner()

    async def explodes(job):
        raise RuntimeError("the tool was on fire")

    job = await runner.start(title="boom", step=explodes)
    await _settle(runner, job)
    assert job.state is JobState.FAILED
    assert "on fire" in job.error


async def test_a_job_never_ends_without_saying_why() -> None:
    """A job that stopped silently is indistinguishable from one still running,
    and "is it still going?" is the only question anyone asks."""
    runner = BackgroundRunner()
    job = await runner.start(title="x", step=_quick)
    await _settle(runner, job)
    assert job.state.is_terminal
    assert job.result or job.error


# ── pause, resume, cancel ────────────────────────────────────────────────────


async def test_a_job_can_be_paused_and_resumed() -> None:
    runner = BackgroundRunner()
    steps: list[int] = []

    async def slow(job):
        steps.append(job.progress.step)
        await asyncio.sleep(0.01)
        return job.progress.step >= 6, ""

    job = await runner.start(title="slow", step=slow)
    await asyncio.sleep(0.02)
    assert runner.pause(job.job_id) is True
    assert job.state is JobState.PAUSED

    frozen = len(steps)
    await asyncio.sleep(0.05)
    assert len(steps) == frozen, "a paused job kept working"

    assert runner.resume(job.job_id) is True
    await _settle(runner, job)
    assert job.state is JobState.COMPLETED


async def test_cancelling_stops_a_job_now() -> None:
    runner = BackgroundRunner()
    job = await runner.start(title="loop", step=_forever, max_steps=10_000)
    await asyncio.sleep(0.01)

    assert runner.cancel(job.job_id) is True
    await asyncio.sleep(0.02)
    assert job.state is JobState.CANCELLED


async def test_a_paused_job_still_wakes_up_to_be_cancelled() -> None:
    """Otherwise "pause" would be a way to make a job uncancellable."""
    runner = BackgroundRunner()
    job = await runner.start(title="loop", step=_forever, max_steps=10_000)
    await asyncio.sleep(0.01)
    runner.pause(job.job_id)

    assert runner.cancel(job.job_id) is True
    await asyncio.sleep(0.02)
    assert job.state is JobState.CANCELLED


async def test_a_cancelled_job_cannot_report_itself_completed() -> None:
    """The state is set before the asyncio cancellation, so a job blocked in
    ``await`` cannot win the race on its way out."""
    runner = BackgroundRunner()

    async def sleeper(job):
        await asyncio.sleep(5)
        return True, "finished after all"  # pragma: no cover

    job = await runner.start(title="sleeper", step=sleeper)
    await asyncio.sleep(0.01)
    runner.cancel(job.job_id)
    await asyncio.sleep(0.02)
    assert job.state is JobState.CANCELLED
    assert job.result == ""


# ── capacity and the stop ────────────────────────────────────────────────────


async def test_capacity_is_refused_rather_than_queued() -> None:
    """A queue would make the limit invisible.

    Work would still be accepted and the user would discover the backlog by
    noticing that nothing had happened.
    """
    runner = BackgroundRunner(max_concurrent=2)
    a = await runner.start(title="a", step=_forever, max_steps=10_000)
    b = await runner.start(title="b", step=_forever, max_steps=10_000)

    with pytest.raises(JobRejected) as caught:
        await runner.start(title="c", step=_quick)
    assert "limit" in str(caught.value)

    runner.cancel(a.job_id)
    runner.cancel(b.job_id)


async def test_nothing_starts_while_the_stop_is_engaged() -> None:
    class _Stop:
        engaged = True

    runner = BackgroundRunner(emergency_stop=_Stop())
    with pytest.raises(JobRejected) as caught:
        await runner.start(title="a", step=_quick)
    assert "emergency stop" in str(caught.value)


async def test_the_stop_ends_jobs_already_running() -> None:
    """A stop that leaves background work running is not a stop."""

    class _Stop:
        engaged = False

    stop = _Stop()
    runner = BackgroundRunner(emergency_stop=stop)
    job = await runner.start(title="loop", step=_forever, max_steps=10_000)
    await asyncio.sleep(0.01)

    stop.engaged = True
    await _settle(runner, job)
    assert job.state is JobState.FAILED
    assert "emergency stop" in job.error


# ── the one that matters ─────────────────────────────────────────────────────


async def test_a_job_stops_when_a_tool_needs_approval() -> None:
    """Unattended execution does not grant authority.

    It removes the person who would have been asked. The only safe answer is to
    stop and wait — a background job that auto-approved because nobody was
    watching would make "run it in the background" a way to escape every
    confirmation in the system.
    """
    runner = BackgroundRunner()
    attempts: list[str] = []

    async def wants_approval(job):
        attempts.append("tried")
        if job.confirmation_id is None:
            runner.needs_confirmation(job.job_id, "confirm_abc")
            return False, "waiting for approval"
        return True, "approved and done"

    job = await runner.start(title="send an email", step=wants_approval)
    await asyncio.sleep(0.05)

    assert job.state is JobState.AWAITING_CONFIRMATION
    assert job.confirmation_id == "confirm_abc"
    frozen = len(attempts)
    await asyncio.sleep(0.05)
    assert len(attempts) == frozen, "the job kept working while awaiting approval"

    runner.cancel(job.job_id)


async def test_approving_lets_the_job_carry_on() -> None:
    runner = BackgroundRunner()

    async def wants_approval(job):
        if job.confirmation_id is not None:
            return False, "still pending"
        if job.progress.step == 1:
            runner.needs_confirmation(job.job_id, "confirm_abc")
            return False, "waiting"
        return True, "done"

    job = await runner.start(title="send an email", step=wants_approval)
    await asyncio.sleep(0.03)
    assert job.state is JobState.AWAITING_CONFIRMATION

    assert runner.resume(job.job_id) is True
    await _settle(runner, job)
    assert job.state is JobState.COMPLETED
    assert job.confirmation_id is None


# ── bookkeeping ──────────────────────────────────────────────────────────────


async def test_finished_jobs_stay_visible_for_a_while() -> None:
    """"What happened to that thing I asked for?" must survive the job ending."""
    runner = BackgroundRunner()
    job = await runner.start(title="x", step=_quick)
    await _settle(runner, job)
    assert runner.get(job.job_id) is not None
    assert job.describe()["state"] == "COMPLETED"


async def test_finished_jobs_do_not_accumulate_forever() -> None:
    """A long-lived daemon must not keep every job it has ever run."""
    runner = BackgroundRunner(max_concurrent=50)
    for index in range(30):
        job = await runner.start(title=f"job {index}", step=_quick)
        await _settle(runner, job)
    assert len(runner.list()) <= 21


def test_a_terminal_state_is_terminal() -> None:
    assert JobState.COMPLETED.is_terminal
    assert JobState.CANCELLED.is_terminal
    assert JobState.FAILED.is_terminal
    assert not JobState.RUNNING.is_terminal
    assert not JobState.PAUSED.is_terminal
    # Awaiting approval is *not* terminal: the job resumes when the user
    # answers, and marking it finished would lose the work already done.
    assert not JobState.AWAITING_CONFIRMATION.is_terminal


def test_a_budget_is_shared_with_the_agent_supervisor() -> None:
    """One budget type, not two.

    A second implementation would drift, and the difference would be invisible
    until a background job outlived a limit a delegated agent respected.
    """
    from jarvis.agents.background import Job

    assert isinstance(Job(job_id="j", task_id=None, title="t").budget, AgentBudget)
