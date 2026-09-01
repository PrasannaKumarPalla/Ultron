"""Command-line interface for the local assistant distribution."""

from __future__ import annotations

import click

import bujji
from bujji.brand import get_branding
from bujji.cli._bootstrap import bootstrap_cmd
from bujji.cli.add_cmd import add
from bujji.cli.agent_cmd import agent
from bujji.cli.ask import ask
from bujji.cli.brand_cmd import brand
from bujji.cli.channel_cmd import channel
from bujji.cli.channels_cmd import channels
from bujji.cli.chat_cmd import chat
from bujji.cli.compose_cmd import compose
from bujji.cli.config_cmd import config
from bujji.cli.connect_cmd import connect
from bujji.cli.daemon_cmd import restart, start, status, stop
from bujji.cli.desktop_cmd import desktop
from bujji.cli.digest_cmd import digest
from bujji.cli.doctor_cmd import doctor
from bujji.cli.gateway_cmd import gateway
from bujji.cli.host_cmd import host
from bujji.cli.init_cmd import init
from bujji.cli.memory_cmd import memory
from bujji.cli.model import model
from bujji.cli.operators_cmd import operators
from bujji.cli.quickstart_cmd import quickstart
from bujji.cli.registry_cmd import registry
from bujji.cli.scan_cmd import scan
from bujji.cli.scheduler_cmd import scheduler
from bujji.cli.self_update_cmd import self_update
from bujji.cli.serve import serve
from bujji.cli.skill_cmd import skill
from bujji.cli.telemetry_cmd import telemetry
from bujji.cli.tool_cmd import tool
from bujji.cli.vault_cmd import vault
from bujji.cli.voice_cmd import voice
from bujji.cli.workflow_cmd import workflow


_BRAND = get_branding()


@click.group(
    help=f"{_BRAND.product_name} â€” modular AI assistant backend",
    invoke_without_command=True,
)
@click.version_option(version=bujji.__version__, prog_name=_BRAND.command_name)
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging")
@click.option("--quiet", is_flag=True, default=False, help="Suppress non-error output")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """Top-level CLI group."""
    from bujji.cli.log_config import setup_logging

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    setup_logging(verbose=verbose, quiet=quiet)

    # Check for updates on interactive commands. The banner is noise in
    # demo recordings of ``bujji ask --research``, so skip it whenever
    # the research flag is in argv (cheap argv sniff â€” Click hasn't
    # parsed the subcommand's args yet at this point).
    import sys

    research_mode_active = "--research" in sys.argv
    if not quiet and ctx.invoked_subcommand and not research_mode_active:
        import threading

        from bujji.cli._version_check import check_for_updates

        # Run the PyPI version poll off the hot path: on a cache miss it does
        # a blocking urlopen (up to 3s) that otherwise delays every command,
        # notably `bujji serve` startup (#263). It's best-effort and never
        # raises, and the nudge prints to stderr, so a daemon thread is safe â€”
        # for long-lived commands (serve) it finishes; for short commands that
        # exit first, the check is simply skipped this run (same as a miss).
        threading.Thread(
            target=check_for_updates,
            args=(ctx.invoked_subcommand,),
            daemon=True,
        ).start()

    # First-run guard â€” routes bare `bujji` to chat or init.
    if ctx.invoked_subcommand is None:
        from bujji.cli._first_run import check_and_route

        check_and_route(ctx)


cli.add_command(init, "init")
cli.add_command(ask, "ask")
cli.add_command(chat, "chat")
cli.add_command(serve, "serve")
cli.add_command(desktop, "desktop")
cli.add_command(model, "model")
cli.add_command(memory, "memory")
cli.add_command(telemetry, "telemetry")
cli.add_command(brand, "brand")
cli.add_command(channel, "channel")
cli.add_command(channels, "channels")
cli.add_command(scheduler, "scheduler")
cli.add_command(doctor, "doctor")
cli.add_command(agent, "agents")
cli.add_command(workflow, "workflow")
cli.add_command(skill, "skill")
cli.add_command(start, "start")
cli.add_command(stop, "stop")
cli.add_command(restart, "restart")
cli.add_command(status, "status")
cli.add_command(vault, "vault")
cli.add_command(add, "add")
cli.add_command(operators, "operators")
cli.add_command(host, "host")
cli.add_command(quickstart, "quickstart")
cli.add_command(compose, "compose")
cli.add_command(gateway, "gateway")
cli.add_command(tool, "tool")
cli.add_command(registry, "registry")
cli.add_command(config, "config")
cli.add_command(scan, "scan")
cli.add_command(connect, "connect")
cli.add_command(digest, "digest")
# deep-research setup pulls the ingestion pipeline (embeddings/numpy). Guard it
# so a broken or slow numpy on Windows â€” which can raise at IMPORT time, not
# just ImportError (#404) â€” can never take down the whole CLI, including
# `bujji serve`. Invoking `bujji deep-research-setup` without the deps still
# errors clearly on demand.
try:
    from bujji.cli.deep_research_setup_cmd import deep_research_setup

    cli.add_command(deep_research_setup, "deep-research-setup")
    cli.add_command(deep_research_setup, "research")
except Exception as _dr_exc:
    import logging as _logging

    _logging.getLogger(__name__).debug("deep-research command unavailable: %s", _dr_exc)
cli.add_command(voice, "voice")
cli.add_command(self_update, "self-update")
cli.add_command(bootstrap_cmd, "_bootstrap")

# Gateway CLI commands (lazy import to avoid pulling starlette)
try:
    from bujji.cli.auth_cmd import auth

    cli.add_command(auth, "auth")
except ImportError:
    pass

try:
    from bujji.cli.tunnel_cmd import tunnel

    cli.add_command(tunnel, "tunnel")
except ImportError:
    pass


def main() -> None:
    """Entry point registered as the console script."""
    import sys

    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            if hasattr(_stream, "reconfigure"):
                try:
                    _stream.reconfigure(encoding="utf-8", errors="replace")
                except (AttributeError, OSError):
                    pass
    cli()


__all__ = ["cli", "main"]
