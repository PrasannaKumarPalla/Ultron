"""``bujji compose`` â€” unified composition CLI for discrete agents and operators."""

from __future__ import annotations

import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table


@click.group()
def compose() -> None:
    """Compose, run, and deploy assistant configurations.

    Recipes are unified TOML configs that wire all five primitives
    (Intelligence, Engine, Agent, Tools, Learning).  They come in two
    kinds:

    \b
      discrete   One-shot or benchmark-oriented agents
      operator   Persistent, scheduled autonomous agents
    """


# ------------------------------------------------------------------ #
# bujji compose list
# ------------------------------------------------------------------ #


@compose.command("list")
@click.option(
    "-k",
    "--kind",
    "kind",
    default=None,
    type=click.Choice(["discrete", "operator"]),
    help="Filter by recipe kind.",
)
def compose_list(kind: Optional[str]) -> None:
    """List all discovered compositions (recipes and operators)."""
    console = Console(stderr=True)
    try:
        from bujji.recipes.loader import discover_recipes

        recipes = discover_recipes(kind=kind)
        if not recipes:
            console.print("[dim]No compositions found.[/dim]")
            console.print(
                "[dim]Place TOML recipes in src/bujji/recipes/data/ "
                "or ~/.bujji/recipes/[/dim]"
            )
            return

        table = Table(title="Compositions", border_style="bright_blue")
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Kind", style="yellow")
        table.add_column("Model", style="green")
        table.add_column("Agent", style="magenta")
        table.add_column("Tools", style="white")
        table.add_column("Description")

        for r in sorted(recipes, key=lambda r: (r.kind, r.name)):
            tools_str = ", ".join(r.tools[:3])
            if len(r.tools) > 3:
                tools_str += f" (+{len(r.tools) - 3})"
            table.add_row(
                r.name,
                r.kind,
                r.model or "-",
                r.agent_type or "-",
                tools_str or "-",
                r.description[:60] if r.description else "",
            )

        console.print(table)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


# ------------------------------------------------------------------ #
# bujji compose show
# ------------------------------------------------------------------ #


@compose.command("show")
@click.argument("name")
def compose_show(name: str) -> None:
    """Show detailed configuration of a composition."""
    console = Console(stderr=True)
    try:
        from bujji.recipes.loader import resolve_recipe

        recipe = resolve_recipe(name)
        if recipe is None:
            console.print(f"[red]Composition not found: {name}[/red]")
            return

        console.print(f"[bold cyan]{recipe.name}[/bold cyan]  ({recipe.kind})")
        console.print(f"  {recipe.description}\n")

        # Intelligence
        console.print("[bold]Intelligence[/bold]")
        console.print(f"  model:        {recipe.model or '-'}")
        console.print(f"  quantization: {recipe.quantization or '-'}")
        console.print(f"  provider:     {recipe.provider or '-'}")
        console.print()

        # Engine
        console.print("[bold]Engine[/bold]")
        console.print(f"  key: {recipe.engine_key or '-'}")
        console.print()

        # Agent
        console.print("[bold]Agent[/bold]")
        console.print(f"  type:        {recipe.agent_type or '-'}")
        console.print(f"  max_turns:   {recipe.max_turns or '-'}")
        console.print(f"  temperature: {recipe.temperature or '-'}")
        console.print(f"  tools:       {', '.join(recipe.tools) or '-'}")
        if recipe.system_prompt:
            preview = recipe.system_prompt[:120].replace("\n", " ")
            console.print(f"  prompt:      {preview}...")
        console.print()

        # Learning
        console.print("[bold]Learning[/bold]")
        console.print(f"  routing: {recipe.routing_policy or '-'}")
        console.print(f"  agent:   {recipe.agent_policy or '-'}")
        console.print()

        # Kind-specific sections
        if recipe.kind == "discrete":
            benchmarks = recipe.eval_benchmarks or recipe.eval_suites
            if benchmarks:
                console.print("[bold]Eval[/bold]")
                console.print(f"  benchmarks:   {', '.join(benchmarks)}")
                console.print(f"  backend:      {recipe.eval_backend or 'auto'}")
                console.print(f"  max_samples:  {recipe.eval_max_samples or 'all'}")
                console.print(f"  judge_model:  {recipe.eval_judge_model or 'default'}")
        elif recipe.kind == "operator":
            if recipe.schedule_type:
                console.print("[bold]Schedule[/bold]")
                console.print(f"  type:  {recipe.schedule_type}")
                console.print(f"  value: {recipe.schedule_value}")
            if recipe.channels:
                console.print("[bold]Channels[/bold]")
                console.print(f"  output: {', '.join(recipe.channels)}")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


# ------------------------------------------------------------------ #
# bujji compose run
# ------------------------------------------------------------------ #


@compose.command("run")
@click.argument("name")
@click.argument("query", nargs=-1, required=True)
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON result.")
def compose_run(name: str, query: tuple[str, ...], output_json: bool) -> None:
    """Run a composition against a single query."""
    console = Console(stderr=True)
    query_text = " ".join(query)

    try:
        from bujji.recipes.loader import resolve_recipe

        recipe = resolve_recipe(name)
        if recipe is None:
            console.print(f"[red]Composition not found: {name}[/red]")
            sys.exit(1)

        kwargs = recipe.to_builder_kwargs()
        console.print(
            f"[dim]Running [cyan]{recipe.name}[/cyan] "
            f"({recipe.agent_type or 'direct'} / "
            f"{recipe.model or 'default'})...[/dim]"
        )

        from bujji.system import SystemBuilder

        builder = SystemBuilder()
        if "engine_key" in kwargs:
            builder = builder.engine(kwargs["engine_key"])
        if "model" in kwargs:
            builder = builder.model(kwargs["model"])
        if "agent" in kwargs:
            builder = builder.agent(kwargs["agent"])
        if "tools" in kwargs:
            builder = builder.tools(kwargs["tools"])

        system = builder.build()

        try:
            agent_kwargs = {}
            if kwargs.get("system_prompt"):
                agent_kwargs["system_prompt"] = kwargs["system_prompt"]
            if kwargs.get("temperature"):
                agent_kwargs["temperature"] = kwargs["temperature"]

            result = system.ask(query_text, **agent_kwargs)

            if output_json:
                import json as json_mod

                if isinstance(result, dict):
                    click.echo(json_mod.dumps(result, indent=2, default=str))
                elif isinstance(result, str):
                    click.echo(json_mod.dumps({"content": result}, indent=2))
                else:
                    click.echo(
                        json_mod.dumps(
                            {
                                "content": result.content,
                                "turns": getattr(result, "turns", 1),
                            },
                            indent=2,
                        )
                    )
            else:
                if isinstance(result, dict):
                    content = result.get("content", "")
                elif isinstance(result, str):
                    content = result
                else:
                    content = result.content
                click.echo(content)
        finally:
            system.close()
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ------------------------------------------------------------------ #
# bujji compose deploy
# ------------------------------------------------------------------ #


@compose.command("deploy")
@click.argument("name")
def compose_deploy(name: str) -> None:
    """Deploy an operator composition (activate its scheduler task)."""
    console = Console(stderr=True)

    try:
        from bujji.recipes.loader import resolve_recipe

        recipe = resolve_recipe(name)
        if recipe is None:
            console.print(f"[red]Composition not found: {name}[/red]")
            sys.exit(1)

        if recipe.kind != "operator":
            console.print(
                f"[red]Recipe '{name}' is a {recipe.kind} composition, "
                f"not an operator.  Only operators can be deployed.[/red]"
            )
            sys.exit(1)

        manifest = recipe.to_operator_manifest()

        from bujji.operators.manager import OperatorManager
        from bujji.system import SystemBuilder

        system = SystemBuilder().scheduler(True).sessions(True).build()
        manager = OperatorManager(system)
        system.operator_manager = manager

        manager.register(manifest)
        task_id = manager.activate(manifest.id)

        console.print(
            f"[green]Deployed operator [cyan]{name}[/cyan] "
            f"(task: {task_id}, schedule: "
            f"{recipe.schedule_type}={recipe.schedule_value})[/green]"
        )
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ------------------------------------------------------------------ #
# bujji compose stop
# ------------------------------------------------------------------ #


@compose.command("stop")
@click.argument("name")
def compose_stop(name: str) -> None:
    """Stop a deployed operator composition."""
    console = Console(stderr=True)

    try:
        from bujji.operators.manager import OperatorManager
        from bujji.system import SystemBuilder

        system = SystemBuilder().scheduler(True).sessions(True).build()
        manager = OperatorManager(system)
        system.operator_manager = manager

        # Discover all known operators so the manager knows about them
        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.recipes.loader import _PROJECT_OPERATORS_DIR

        for d in [DEFAULT_CONFIG_DIR / "operators", _PROJECT_OPERATORS_DIR]:
            if d.is_dir():
                manager.discover(d)

        manager.deactivate(name)
        console.print(f"[yellow]Stopped operator {name}[/yellow]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ------------------------------------------------------------------ #
# bujji compose status
# ------------------------------------------------------------------ #


@compose.command("status")
def compose_status() -> None:
    """Show status of all deployed operators."""
    console = Console(stderr=True)

    try:
        from bujji.operators.manager import OperatorManager
        from bujji.system import SystemBuilder

        system = SystemBuilder().scheduler(True).sessions(True).build()
        manager = OperatorManager(system)
        system.operator_manager = manager

        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.recipes.loader import _PROJECT_OPERATORS_DIR

        for d in [DEFAULT_CONFIG_DIR / "operators", _PROJECT_OPERATORS_DIR]:
            if d.is_dir():
                manager.discover(d)

        statuses = manager.status()
        if not statuses:
            console.print("[dim]No operators registered.[/dim]")
            return

        table = Table(title="Operator Status", border_style="bright_blue")
        table.add_column("Name", style="cyan")
        table.add_column("State", style="yellow")
        table.add_column("Schedule", style="white")
        table.add_column("Last Run", style="dim")

        for s in statuses:
            table.add_row(
                s.get("id", "?"),
                s.get("state", "unknown"),
                s.get("schedule", ""),
                s.get("last_run", "-"),
            )
        console.print(table)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


__all__ = ["compose"]
