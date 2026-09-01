"""Startup banner driven by the user-facing brand configuration."""

from __future__ import annotations

from bujji.brand import get_branding


def print_banner(quiet: bool = False) -> None:
    """Print the configured startup banner. No-op when quiet."""
    if quiet:
        return
    brand = get_branding()
    try:
        from rich.console import Console

        console = Console()
        console.print(
            brand.product_name,
            style="bold bright_blue",
            highlight=False,
            markup=False,
        )
        console.print(brand.tagline, style="cyan", highlight=False, markup=False)
        console.print()
    except ImportError:
        print(brand.product_name)
        print(brand.tagline)
        print()
