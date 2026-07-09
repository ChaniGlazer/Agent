"""Command-line entry point for the agent.

Example:
    python -m agent.main --config config.yaml --goal "Fill out the contact form and submit it"
"""

from __future__ import annotations

import argparse
import logging
import sys

from agent.browser import BrowserConnectionError, BrowserManager
from agent.config import AgentConfig, ApprovalMode
from agent.controller import AgentController
from agent.llm import create_llm_provider
from agent.logger import setup_logger
from agent.memory import Memory
from agent.tools import ToolExecutor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Playwright browser agent on a single internal website."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to the YAML configuration file.")
    parser.add_argument("--goal", required=True, help="Natural-language description of the task to accomplish.")
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Override config: don't perform mutating actions, only report what would happen.",
    )
    parser.add_argument(
        "--approval-mode", choices=[m.value for m in ApprovalMode], default=None,
        help="Override config: human approval mode.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one agent task end-to-end. Returns a process exit code."""
    args = parse_args(argv)
    config = AgentConfig.from_yaml(args.config)

    if args.dry_run is not None:
        config.dry_run = args.dry_run
    if args.approval_mode is not None:
        config.approval_mode = ApprovalMode(args.approval_mode)

    config.ensure_directories()
    logger = setup_logger(config.log_folder)
    logger.info("Loaded configuration from %s", args.config)

    memory = Memory(goal=args.goal)
    result = None

    try:
        with BrowserManager(config.remote_debug_port, config.timeout_ms) as browser_manager:
            page = browser_manager.get_page(url_contains=config.target_url)
            tools = ToolExecutor(page=page, config=config, memory=memory)
            llm = create_llm_provider(config)
            controller = AgentController(config=config, llm=llm, tools=tools, memory=memory)
            result = controller.run(args.goal)
    except BrowserConnectionError as exc:
        logger.error(str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        memory.save(config.data_folder)

    logger.info("Task finished: completed=%s steps=%d reason=%s", result.completed, result.steps_taken, result.stop_reason)
    print(f"Completed: {result.completed}\nSteps: {result.steps_taken}\nReason: {result.stop_reason}")
    return 0 if result.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
