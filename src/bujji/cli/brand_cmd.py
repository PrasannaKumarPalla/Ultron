"""Manage user-local B.U.J.J.I branding overrides."""

from __future__ import annotations

import click

from bujji.brand import (
    get_branding,
    get_user_brand_file,
    reset_user_branding,
    save_user_branding,
)


@click.group("brand")
def brand() -> None:
    """Show or update user-specific product branding."""


@brand.command("show")
def show_brand() -> None:
    """Print the effective branding values."""
    branding = get_branding()
    click.echo(f"Product name : {branding.product_name}")
    click.echo(f"Display name : {branding.display_name}")
    click.echo(f"Command name : {branding.command_name}")
    click.echo(f"Wake word    : {branding.wake_word}")
    click.echo(f"Override file: {get_user_brand_file()}")


@brand.command("set")
@click.option("--name", "product_name", help="Visible product/app name.")
@click.option("--display-name", help="Decorative UI label, e.g. B.U.J.J.I.")
@click.option("--command-name", help="CLI command name, e.g. bujji.")
@click.option("--wake-word", help="Default wake word for voice detection.")
def set_brand(
    product_name: str | None,
    display_name: str | None,
    command_name: str | None,
    wake_word: str | None,
) -> None:
    """Persist user-local branding overrides."""
    overrides = {
        "product_name": product_name,
        "display_name": display_name,
        "command_name": command_name.lower() if command_name else None,
        "wake_word": wake_word.lower() if wake_word else None,
    }
    if not any(overrides.values()):
        raise click.ClickException("Pass at least one option to update branding.")

    path = save_user_branding(overrides)
    branding = get_branding()
    click.echo(f"Saved branding overrides to {path}")
    click.echo(
        f"{branding.product_name} is now listening for "
        f'"{branding.wake_word}" and using command "{branding.command_name}".'
    )


@brand.command("reset")
def reset_brand() -> None:
    """Remove user-local branding overrides."""
    path = reset_user_branding()
    branding = get_branding()
    click.echo(f"Removed branding overrides from {path}")
    click.echo(
        f"Reverted to {branding.product_name} / {branding.display_name} "
        f'with wake word "{branding.wake_word}".'
    )
