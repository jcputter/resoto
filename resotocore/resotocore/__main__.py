import asyncio
import logging
import platform
import ssl
import sys
from argparse import Namespace
from asyncio import Queue
from contextlib import suppress
from datetime import timedelta
from ssl import SSLContext
from typing import AsyncIterator, Optional, List

from aiohttp.web_app import Application
from arango.database import StandardDatabase

from resotocore import version
from resotocore.analytics import CoreEvent, NoEventSender
from resotocore.analytics.posthog import PostHogEventSender
from resotocore.analytics.recurrent_events import emit_recurrent_events
from resotocore.cli.cli import CLI
from resotocore.cli.command import alias_names, all_commands
from resotocore.cli.model import CLIDependencies
from resotocore.config.config_handler_service import ConfigHandlerService
from resotocore.config.core_config_handler import CoreConfigHandler
from resotocore.core_config import config_from_db, CoreConfig
from resotocore.db import SystemData
from resotocore.db.db_access import DbAccess
from resotocore.dependencies import db_access, setup_process, parse_args, system_info, reconfigure_logging
from resotocore.error import RestartService
from resotocore.message_bus import MessageBus
from resotocore.model.model_handler import ModelHandlerDB
from resotocore.model.typed_model import to_json, class_fqn
from resotocore.query.template_expander import DBTemplateExpander
from resotocore.task.scheduler import Scheduler
from resotocore.task.subscribers import SubscriptionHandler
from resotocore.task.task_handler import TaskHandlerService
from resotocore.util import shutdown_process, utc
from resotocore.web import runner
from resotocore.web.api import Api
from resotocore.web.certificate_handler import CertificateHandler
from resotocore.worker_task_queue import WorkerTaskQueue

log = logging.getLogger(__name__)


def main() -> None:
    """
    Application entrypoint - no arguments are allowed.
    """
    try:
        run(sys.argv[1:])
        log.info("Process finished.")
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopping resoto graph core.")
        shutdown_process(0)
    except Exception as ex:
        print(f"resotocore stopped. Reason {class_fqn(ex)}: {ex}", file=sys.stderr)
        shutdown_process(1)


def run(arguments: List[str]) -> None:
    """
    Run application. When this method returns, the process is done.
    :param arguments: the arguments provided to this process.
                 Note: this method is used in tests to specify arbitrary arguments.
    """
    args = parse_args(arguments)
    setup_process(args)

    # after setup, logging is possible
    info = system_info()
    log.info(
        f"Starting up version={info.version} on system with cpus={info.cpus}, "
        f"available_mem={info.mem_available}, total_mem={info.mem_total}"
    )

    # The loop is here to restart the process in case of RestartService exceptions.
    while True:
        try:
            run_process(args)
            break  # This line should never be reached. In case it does, break the loop.
        except RestartService as ex:
            message = f"Restarting Service. Reason: {ex.reason}"
            line = "-" * len(message)
            print(f"\n{line}\n{message}\n{line}\n")


def run_process(args: Namespace) -> None:
    # wait here for an initial connection to the database before we continue. blocking!
    created, system_data, sdb = DbAccess.connect(args, timedelta(seconds=60))
    config = config_from_db(args, sdb)
    with_config(created, system_data, sdb, config)


def with_config(created: bool, system_data: SystemData, sdb: StandardDatabase, config: CoreConfig) -> None:
    reconfigure_logging(config)  # based on the config, logging might have changed
    info = system_info()
    event_sender = NoEventSender() if config.runtime.analytics_opt_out else PostHogEventSender(system_data)
    db = db_access(config, sdb, event_sender)
    cert_handler = CertificateHandler.lookup(config, sdb)
    message_bus = MessageBus()
    scheduler = Scheduler()
    worker_task_queue = WorkerTaskQueue()
    model = ModelHandlerDB(db.get_model_db(), config.runtime.plantuml_server)
    template_expander = DBTemplateExpander(db.template_entity_db)
    config_handler = ConfigHandlerService(
        db.config_entity_db,
        db.config_validation_entity_db,
        db.configs_model_db,
        worker_task_queue,
        message_bus,
    )
    cli_deps = CLIDependencies(
        message_bus=message_bus,
        event_sender=event_sender,
        db_access=db,
        model_handler=model,
        worker_task_queue=worker_task_queue,
        config=config,
        template_expander=template_expander,
        config_handler=config_handler,
    )
    default_env = {"graph": config.cli.default_graph, "section": config.cli.default_section}
    cli = CLI(cli_deps, all_commands(cli_deps), default_env, alias_names())
    subscriptions = SubscriptionHandler(db.subscribers_db, message_bus)
    task_handler = TaskHandlerService(
        db.running_task_db, db.job_db, message_bus, event_sender, subscriptions, scheduler, cli, config
    )
    core_config_handler = CoreConfigHandler(config, message_bus, worker_task_queue, config_handler)
    cli_deps.extend(task_handler=task_handler)
    api = Api(
        db,
        model,
        subscriptions,
        task_handler,
        message_bus,
        event_sender,
        worker_task_queue,
        cert_handler,
        config_handler,
        cli,
        template_expander,
        config,
    )
    event_emitter = emit_recurrent_events(
        event_sender, model, subscriptions, worker_task_queue, message_bus, timedelta(hours=1), timedelta(hours=1)
    )

    async def on_start() -> None:
        # queue must be created inside an async function!
        cli_deps.extend(forked_tasks=Queue())
        await db.start()
        await event_sender.start()
        await subscriptions.start()
        await scheduler.start()
        await worker_task_queue.start()
        await event_emitter.start()
        await cli.start()
        await task_handler.start()
        await core_config_handler.start()
        await api.start()
        if created:
            await event_sender.core_event(CoreEvent.SystemInstalled)
        await event_sender.core_event(
            CoreEvent.SystemStarted,
            {
                "version": version(),
                "created_at": to_json(system_data.created_at),
                "system": platform.system(),
                "platform": platform.platform(),
                "inside_docker": info.inside_docker,
            },
            cpu_count=info.cpus,
            mem_total=info.mem_total,
            mem_available=info.mem_available,
        )

    async def on_stop() -> None:
        duration = utc() - info.started_at
        await api.stop()
        await core_config_handler.stop()
        await task_handler.stop()
        await cli.stop()
        await event_sender.core_event(CoreEvent.SystemStopped, total_seconds=int(duration.total_seconds()))
        await event_emitter.stop()
        await worker_task_queue.stop()
        await scheduler.stop()
        await subscriptions.stop()
        await db.stop()
        await event_sender.stop()

    async def async_initializer() -> Application:
        async def clean_all_tasks() -> None:
            log.info("Clean up all running tasks.")
            for task in asyncio.all_tasks():
                with suppress(asyncio.CancelledError):
                    if not task.done() or not task.cancelled():
                        task.cancel()
                    log.debug(f"Wait for task: {task}")
                    await task

        async def on_start_stop(_: Application) -> AsyncIterator[None]:
            await on_start()
            log.info("Initialization done. Starting API.")
            yield
            log.info("Shutdown initiated. Stop all tasks.")
            await on_stop()
            await clean_all_tasks()

        api.app.cleanup_ctx.append(on_start_stop)
        return api.app

    tls_context: Optional[SSLContext] = None
    if config.args.tls_cert:
        tls_context = SSLContext(ssl.PROTOCOL_TLS)
        tls_context.load_cert_chain(config.args.tls_cert, config.args.tls_key, config.args.tls_password)

    runner.run_app(async_initializer(), api.stop, host=config.api.hosts, port=config.api.port, ssl_context=tls_context)


if __name__ == "__main__":
    main()
