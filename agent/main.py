"""Entry point for running the agent as a server (locally or on Render).

Example:
    python -m agent.main --config config.yaml

On Render, the platform sets the `PORT` environment variable; it takes
precedence over `config.yaml`'s `port` so the service binds where Render's
router expects it.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from agent.config import AgentConfig
from agent.logger import setup_logger
from agent.server import create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Run the web agent's server.")
    parser.add_argument("--config", default="config.yaml", help="Path to the YAML configuration file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Start the FastAPI/uvicorn server. Returns a process exit code."""
    args = parse_args(argv)
    config = AgentConfig.from_yaml(args.config)
    config.ensure_directories()

    logger = setup_logger(config.log_folder)
    logger.info("Loaded configuration from %s", args.config)

    port = int(os.environ.get("PORT", config.port))
    app = create_app(config)

    logger.info("Starting server on %s:%d (target_url=%s)", config.host, port, config.target_url)
    uvicorn.run(app, host=config.host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
