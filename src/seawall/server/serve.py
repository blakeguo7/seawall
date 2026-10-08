"""Running the application under uvicorn."""

from __future__ import annotations

import signal
from types import FrameType

import uvicorn

from seawall.server.app import check_environment, create_app
from seawall.server.config import ServerConfig
from seawall.server.manager import ClientFactory, SessionManager

GRACEFUL_SHUTDOWN_SECONDS = 10


class Server(uvicorn.Server):
    """A uvicorn server that tells the session manager to stop first.

    Without this, a Ctrl-C would wait for every open event stream to be closed by its client
    (streams never end on their own) and then run out the graceful-shutdown timer. Telling the
    manager first wakes the streams so they close, and the runs are interrupted and saved.
    """

    def __init__(self, config: uvicorn.Config, manager: SessionManager) -> None:
        super().__init__(config)
        self._manager = manager

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._manager.request_shutdown()
        super().handle_exit(sig, frame)

    def stop(self) -> None:
        """Shut down as if SIGTERM had arrived, from any thread."""
        self.handle_exit(signal.SIGTERM, None)


def build_server(
    config: ServerConfig,
    *,
    client_factory: ClientFactory | None = None,
    log_level: str = "info",
) -> Server:
    app = create_app(config, client_factory=client_factory)
    return Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level=log_level,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            server_header=False,
        ),
        app.state.manager,
    )


def run_server(config: ServerConfig, *, log_level: str = "info") -> None:
    """Serve until interrupted. Raises ConfigError for a configuration that cannot be served."""
    config.validate()
    check_environment()
    build_server(config, log_level=log_level).run()
