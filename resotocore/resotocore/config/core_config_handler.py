import asyncio
import logging
from asyncio import Task
from contextlib import suppress
from functools import partial
from typing import Optional, List, Callable

import yaml

from resotocore.config import ConfigHandler, ConfigEntity, ConfigValidation
from resotocore.core_config import CoreConfig, ResotoCoreConfigId, config_model, EditableConfig, ResotoCoreRoot
from resotocore.dependencies import empty_config
from resotocore.message_bus import MessageBus, CoreMessage
from resotocore.model.model import Kind
from resotocore.model.typed_model import from_js
from resotocore.util import deep_merge, restart_service, value_in_path
from resotocore.worker_task_queue import WorkerTaskQueue, WorkerTaskDescription, WorkerTaskName, WorkerTask

log = logging.getLogger(__name__)


class CoreConfigHandler:
    def __init__(
        self,
        config: CoreConfig,
        message_bus: MessageBus,
        worker_task_queue: WorkerTaskQueue,
        config_handler: ConfigHandler,
        exit_fn: Callable[[], None] = partial(restart_service, "resotocore config changed."),
    ):
        self.message_bus = message_bus
        self.worker_task_queue = worker_task_queue
        self.config_updated_listener: Optional[Task[None]] = None
        self.config_validator: Optional[Task[None]] = None
        self.config = config
        self.config_handler = config_handler
        self.exit_fn = exit_fn

    async def __validate_config(self) -> None:
        worker_id = "resotocore.config.validate"
        description = WorkerTaskDescription(WorkerTaskName.validate_config, {"config_id": [ResotoCoreConfigId]})
        async with self.worker_task_queue.attach(worker_id, [description]) as tasks:
            while True:
                task: WorkerTask = await tasks.get()
                try:
                    config = value_in_path(task.data, ["config", ResotoCoreRoot])
                    if isinstance(config, dict):
                        # try to read editable config, throws if there are errors
                        from_js(config, EditableConfig)
                        errors = EditableConfig.validate_config(config)
                        if errors:
                            message = "Validation Errors:\n" + yaml.safe_dump(errors)
                            await self.worker_task_queue.error_task(worker_id, task.id, message)
                        else:
                            await self.worker_task_queue.acknowledge_task(worker_id, task.id)
                        continue
                except Exception as ex:
                    log.warning("Error processing validate configuration task", exc_info=ex)
                    await self.worker_task_queue.error_task(worker_id, task.id, str(ex))
                    continue
                # safeguard, if we should ever come here
                await self.worker_task_queue.error_task(worker_id, task.id, "Failing to process the task!")

    async def __handle_events(self) -> None:
        async with self.message_bus.subscribe("resotocore_config_update", [CoreMessage.ConfigUpdated]) as events:
            while True:
                event = await events.get()
                if event.data.get("id") == ResotoCoreConfigId:
                    log.info("Core config was updated. Restart to take effect.")
                    # stop the process and rely on os to restart the service
                    self.exit_fn()

    async def __update_config(self) -> None:
        try:
            # in case the internal configuration holds new properties, we update the existing config always.
            existing = await self.config_handler.get_config(ResotoCoreConfigId)
            empty = empty_config().json()
            updated = deep_merge(empty, existing.config) if existing else empty
            if existing is None or updated != existing.config:
                await self.config_handler.put_config(ConfigEntity(ResotoCoreConfigId, updated), False)
                log.info("Default resoto config updated.")
        except Exception as ex:
            log.error(f"Could not update resoto default configuration: {ex}", exc_info=ex)

    async def __update_model(self) -> None:
        try:
            kinds = from_js(config_model(), List[Kind])
            await self.config_handler.update_configs_model(kinds)
            await self.config_handler.put_config_validation(
                ConfigValidation(ResotoCoreConfigId, external_validation=True)
            )
            log.debug("Resoto core config model updated.")
        except Exception as ex:
            log.error(f"Could not update resoto core config model: {ex}", exc_info=ex)

    async def start(self) -> None:
        await self.__update_model()
        await self.__update_config()
        self.config_updated_listener = asyncio.create_task(self.__handle_events())
        self.config_validator = asyncio.create_task(self.__validate_config())

    async def stop(self) -> None:
        # wait for the spawned task to complete
        if self.config_updated_listener:
            with suppress(Exception):
                self.config_updated_listener.cancel()
        if self.config_validator:
            with suppress(Exception):
                self.config_validator.cancel()
