import logging
from datetime import timedelta
from typing import AsyncGenerator, List

import pytest
from pytest import fixture, LogCaptureFixture

from resotocore.analytics import AnalyticsEventSender
from resotocore.cli.cli import CLI
from resotocore.db.jobdb import JobDb
from resotocore.db.runningtaskdb import RunningTaskDb
from resotocore.dependencies import empty_config
from resotocore.error import ParseError
from resotocore.message_bus import MessageBus, Event, Message, ActionDone, Action
from resotocore.task import TaskHandler
from resotocore.task.model import Subscriber
from resotocore.task.scheduler import Scheduler
from resotocore.task.subscribers import SubscriptionHandler
from resotocore.task.task_description import (
    Workflow,
    Step,
    PerformAction,
    EventTrigger,
    StepErrorBehaviour,
    TimeTrigger,
    Job,
    TaskSurpassBehaviour,
    ExecuteCommand,
)
from resotocore.task.task_handler import TaskHandlerService

# noinspection PyUnresolvedReferences
from tests.resotocore.analytics import event_sender

# noinspection PyUnresolvedReferences
from tests.resotocore.cli.cli_test import cli, cli_deps

# noinspection PyUnresolvedReferences
from tests.resotocore.config.config_handler_service_test import config_handler
from tests.resotocore.db.entitydb import InMemoryDb

# noinspection PyUnresolvedReferences
from tests.resotocore.db.graphdb_test import (
    filled_graph_db,
    graph_db,
    test_db,
    foo_model,
    foo_kinds,
    system_db,
    local_client,
)

# noinspection PyUnresolvedReferences
from tests.resotocore.db.runningtaskdb_test import running_task_db

# noinspection PyUnresolvedReferences
from tests.resotocore.message_bus_test import message_bus, all_events, wait_for_message

# noinspection PyUnresolvedReferences
from tests.resotocore.query.template_expander_test import expander

# noinspection PyUnresolvedReferences
from tests.resotocore.worker_task_queue_test import worker, task_queue, performed_by, incoming_tasks


@fixture
async def subscription_handler(message_bus: MessageBus) -> SubscriptionHandler:
    in_mem = InMemoryDb(Subscriber, lambda x: x.id)
    result = SubscriptionHandler(in_mem, message_bus)
    return result


@fixture
def job_db() -> JobDb:
    return InMemoryDb[Job](Job, lambda x: x.id)


@fixture
async def task_handler(
    running_task_db: RunningTaskDb,
    job_db: JobDb,
    message_bus: MessageBus,
    event_sender: AnalyticsEventSender,
    subscription_handler: SubscriptionHandler,
    cli: CLI,
    test_workflow: Workflow,
) -> AsyncGenerator[TaskHandlerService, None]:
    config = empty_config()
    task_handler = TaskHandlerService(
        running_task_db, job_db, message_bus, event_sender, subscription_handler, Scheduler(), cli, config
    )
    task_handler.task_descriptions = [test_workflow]
    cli.dependencies.lookup["task_handler"] = task_handler
    async with task_handler:
        yield task_handler


@fixture
def test_workflow() -> Workflow:
    return Workflow(
        "test_workflow",
        "Speakable name of workflow",
        [
            Step("start", PerformAction("start_collect"), timedelta(seconds=10)),
            Step("act", PerformAction("collect"), timedelta(seconds=10)),
            Step("done", PerformAction("collect_done"), timedelta(seconds=10), StepErrorBehaviour.Stop),
        ],
        [EventTrigger("start me up"), TimeTrigger("1 1 1 1 1")],
    )


@pytest.mark.asyncio
async def test_run_job(task_handler: TaskHandlerService, all_events: List[Message]) -> None:
    await task_handler.handle_event(Event("start me up"))
    started: Event = await wait_for_message(all_events, "task_started", Event)
    await wait_for_message(all_events, "task_end", Event)
    assert started.data["task"] == "Speakable name of workflow"


@pytest.mark.asyncio
async def test_parse_job_line_time_trigger(task_handler: TaskHandler) -> None:
    job = await task_handler.parse_job_line("test", '0 5 * * sat   match t2 == "node" | clean')
    assert job.trigger == TimeTrigger("0 5 * * sat")
    assert job.command.command == 'match t2 == "node" | clean'
    assert job.wait is None
    with pytest.raises(ParseError):
        await task_handler.parse_job_line("test", '0 5 invalid_cron_expr * sat : match t2 == "node" | clean')


@pytest.mark.asyncio
async def test_parse_job_line_event_and_time_trigger(task_handler: TaskHandler) -> None:
    job = await task_handler.parse_job_line("test", '0 5 * * sat cleanup_plan : match t2 == "node" | clean')
    assert job.trigger == TimeTrigger("0 5 * * sat")
    assert job.command.command == 'match t2 == "node" | clean'
    assert job.wait[0] == EventTrigger("cleanup_plan")  # type: ignore
    with pytest.raises(ParseError):
        await task_handler.parse_job_line("test", '0 5 invalid_cron_expr * sat cleanup_plan:match t2 == "node" | clean')


@pytest.mark.asyncio
async def test_parse_job_line_event_trigger(task_handler: TaskHandler) -> None:
    job = await task_handler.parse_job_line("test", 'cleanup_plan : match t2 == "node" | clean')
    assert job.trigger == EventTrigger("cleanup_plan")
    assert job.command.command == 'match t2 == "node" | clean'
    assert job.wait is None
    with pytest.raises(ParseError):
        await task_handler.parse_job_line("test", 'cleanup_plan match t2 == "node" | clean')


@pytest.mark.asyncio
async def test_recover_workflow(
    running_task_db: RunningTaskDb,
    job_db: JobDb,
    message_bus: MessageBus,
    event_sender: AnalyticsEventSender,
    subscription_handler: SubscriptionHandler,
    all_events: List[Message],
    cli: CLI,
    test_workflow: Workflow,
) -> None:
    def handler() -> TaskHandlerService:
        th = TaskHandlerService(
            running_task_db,
            job_db,
            message_bus,
            event_sender,
            subscription_handler,
            Scheduler(),
            cli,
            empty_config(),
        )
        th.task_descriptions = [test_workflow]
        return th

    await subscription_handler.add_subscription("sub_1", "start_collect", True, timedelta(seconds=30))
    sub1 = await subscription_handler.add_subscription("sub_1", "collect", True, timedelta(seconds=30))
    sub2 = await subscription_handler.add_subscription("sub_2", "collect", True, timedelta(seconds=30))

    async with handler() as wf1:
        # kick off a new workflow
        await wf1.handle_event(Event("start me up"))
        assert len(wf1.tasks) == 1
        # expect a start_collect action message
        a: Action = await wait_for_message(all_events, "start_collect", Action)
        await wf1.handle_action_done(ActionDone(a.message_type, a.task_id, a.step_name, sub1.id, dict(a.data)))

        # expect a collect action message
        b: Action = await wait_for_message(all_events, "collect", Action)
        await wf1.handle_action_done(ActionDone(b.message_type, b.task_id, b.step_name, sub1.id, dict(b.data)))

    # subscriber 3 is also registering for collect
    # since the collect phase is already started, it should not participate in this round
    sub3 = await subscription_handler.add_subscription("sub_3", "collect", True, timedelta(seconds=30))

    # simulate a restart, wf1 is stopped and wf2 needs to recover from database
    async with handler() as wf2:
        assert len(wf2.tasks) == 1
        wfi = list(wf2.tasks.values())[0]
        assert wfi.current_state.name == "act"
        assert (await wf2.list_all_pending_actions_for(sub1)) == []
        assert (await wf2.list_all_pending_actions_for(sub2)) == [Action("collect", wfi.id, "act", {})]
        assert (await wf2.list_all_pending_actions_for(sub3)) == []
        await wf2.handle_action_done(ActionDone("collect", wfi.id, "act", sub2.id, {}))
        # expect an event workflow_end
        await wait_for_message(all_events, "task_end", Event)
        # all workflow instances are gone
    assert len(wf2.tasks) == 0

    # simulate a restart, wf3 should start from a clean slate, since all instances are done
    async with handler() as wf3:
        assert len(wf3.tasks) == 0


@pytest.mark.asyncio
async def test_wait_for_running_job(
    task_handler: TaskHandlerService, test_workflow: Workflow, all_events: List[Message]
) -> None:
    test_workflow.on_surpass = TaskSurpassBehaviour.Wait
    task_handler.task_descriptions = [test_workflow]
    # subscribe as collect handler - the workflow will need to wait for this handler
    sub = await task_handler.subscription_handler.add_subscription("sub_1", "collect", True, timedelta(seconds=30))
    await task_handler.handle_event(Event("start me up"))
    # check, that the workflow has started
    running_before = await task_handler.running_tasks()
    assert len(running_before) == 1
    act: Action = await wait_for_message(all_events, "collect", Action)
    # pull the same trigger: the workflow can not be started, since there is already one in progress -> wait
    await task_handler.handle_event(Event("start me up"))
    # report success of the only subscriber
    await task_handler.handle_action_done(ActionDone("collect", act.task_id, act.step_name, sub.id, dict(act.data)))
    # check overdue tasks: wipe finished tasks and eventually start waiting tasks
    await task_handler.check_overdue_tasks()
    # check, that the workflow has started
    running_after = await task_handler.running_tasks()
    assert len(running_after) == 1
    t_before, t_after = running_before[0], running_after[0]
    assert t_before.descriptor.id == t_after.descriptor.id and t_before.id != t_after.id


@pytest.mark.asyncio
async def test_handle_failing_task_command(task_handler: TaskHandlerService, caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.WARN)
    # This job will fail. Take a very long timeout - to avoid a timeout
    job = Job("fail", ExecuteCommand("non_existing_command"), timedelta(hours=4))
    task_handler.task_descriptions = [job]
    assert len(await task_handler.running_tasks()) == 0
    await task_handler.start_task(job, "test fail")
    assert len(await task_handler.running_tasks()) == 1
    # The task is executed async - let's wait here directly
    update_task = (next(iter(task_handler.tasks.values()))).update_task
    assert update_task
    await update_task
    await task_handler.check_overdue_tasks()
    assert len(await task_handler.running_tasks()) == 0
    # One warning has been emitted
    assert len(caplog.records) == 1
    assert "Command non_existing_command failed with error" in caplog.records[0].message
