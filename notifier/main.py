"""Notifier service entry point."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from notifier.api import create_app
from notifier.matrix_gateway import TchapMatrixGateway, cancel_task
from notifier.repository import NotificationRepository
from notifier.service import NotifierService
from notifier.settings import NotifierSettings

logger = logging.getLogger(__name__)


class NotifierRuntime:
    """Run the API, Matrix sync, and dispatcher in the same event loop."""

    def __init__(self, settings: NotifierSettings) -> None:
        self.settings = settings
        self.repository = NotificationRepository(settings.database_path)
        self.matrix = TchapMatrixGateway(settings)
        self.service = NotifierService(
            self.repository,
            self.matrix,
            dispatch_interval_seconds=settings.dispatch_interval_seconds,
            max_delivery_attempts=settings.max_delivery_attempts,
        )

    async def run(self) -> None:
        try:
            await self._run()
        finally:
            await self.matrix.close()
            self.repository.close()

    async def _run(self) -> None:
        await self.matrix.connect(self.service.handle_membership)
        await self.service.reconcile_memberships()

        app = create_app(
            self.service,
            self.settings.api_token.get_secret_value(),
            lambda: self.matrix.connected,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.settings.api_host,
                port=self.settings.api_port,
                log_level="info",
                access_log=True,
            )
        )

        sync_task = asyncio.create_task(
            self.matrix.sync_forever(),
            name="matrix-sync",
        )
        dispatcher_task = asyncio.create_task(
            self.service.run_dispatcher(),
            name="notification-dispatcher",
        )
        server_task = asyncio.create_task(server.serve(), name="http-server")

        try:
            done, _ = await asyncio.wait(
                {sync_task, server_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sync_task in done and not server.should_exit:
                error = sync_task.exception()
                server.should_exit = True
                await server_task
                if error:
                    raise error
                raise RuntimeError("The Matrix synchronization loop stopped")
        finally:
            server.should_exit = True
            self.service.stop()
            for task in (server_task, sync_task, dispatcher_task):
                if not task.done():
                    await cancel_task(task)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = NotifierSettings()  # pyright: ignore[reportCallIssue]
    logger.info("Starting Notifier", extra=settings.safe_summary())
    asyncio.run(NotifierRuntime(settings).run())


if __name__ == "__main__":
    main()
