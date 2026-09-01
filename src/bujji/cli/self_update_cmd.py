"""`assistant self-update` - upgrade the local assistant checkout."""

from __future__ import annotations

import shlex
import subprocess
import sys

import click

import bujji
from bujji.cli._install_detect import detect_install


@click.command(
    "self-update",
    help=(
        "Upgrade the assistant checkout. Detects how you "
        "installed it and runs the right command. "
        "Use --check to only print the upgrade command."
    ),
)
@click.option(
    "--check",
    is_flag=True,
    help="Print the upgrade command that would run, without executing it.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
def self_update(check: bool, yes: bool) -> None:
    info = detect_install()
    current = bujji.__version__

    click.echo(f"Current assistant version: v{current}")
    click.echo(f"Install method: {info.kind}")
    click.echo(f"Upgrade command: {info.upgrade_command}")

    if current.endswith("+unknown") or current.startswith("0.0.0"):
        click.echo(
            "\nThis looks like a local fork/editable checkout. "
            "The safe update path is to pull your repo and re-run sync."
        )

    if check:
        return

    if info.kind == "unknown":
        click.echo(
            "\nCould not determine install method with confidence. "
            "The command above is a best guess; verify it matches how you "
            "installed before running.",
            err=True,
        )

    if not yes:
        if not click.confirm("\nRun the upgrade command now?", default=True):
            click.echo("Aborted.")
            sys.exit(1)

    click.echo(f"\n-> {info.upgrade_command}\n")

    if info.kind == "editable-git":
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true  -- this is the shell-exec / sandbox runner — a shell command string is the input by design
        result = subprocess.run(info.upgrade_command, shell=True)
    else:
        result = subprocess.run(shlex.split(info.upgrade_command))

    if result.returncode != 0:
        click.echo(
            f"\nUpgrade command exited with code {result.returncode}. "
            "Inspect the output above for the failure mode.",
            err=True,
        )
        sys.exit(result.returncode)

    click.echo("\nUpgrade complete. Re-run `assistant --version` to confirm.")
