"""``bujji init`` â€” detect hardware, generate config, write to disk."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import click
import httpx
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from bujji.brand import get_branding
from bujji.cli._banner import print_banner
from bujji.cli._bootstrap import detect_cloud_keys
from bujji.cli.model import find_model_spec, hf_download, ollama_pull
from bujji.cli.scan_cmd import PrivacyScanner
from bujji.core.config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    _available_memory_gb,
    detect_hardware,
    estimated_download_gb,
    generate_default_toml,
    generate_minimal_toml,
    recommend_engine,
    recommend_model,
)

# Engines supported by ``bujji init --engine``.
_SUPPORTED_ENGINES = [
    "ollama",
    "vllm",
    "sglang",
    "llamacpp",
    "mlx",
    "lmstudio",
    "exo",
    "nexa",
]

_BRAND = get_branding()


def _detect_running_engines() -> list[str]:
    """Probe well-known ports and return engine keys that respond."""
    import httpx

    _PROBES: dict[str, str] = {
        "ollama": "http://localhost:11434/api/tags",
        "vllm": "http://localhost:8000/v1/models",
        "sglang": "http://localhost:30000/v1/models",
        "llamacpp": "http://localhost:8080/v1/models",
        "mlx": "http://localhost:8080/v1/models",
        "lmstudio": "http://localhost:1234/v1/models",
        "exo": "http://localhost:52415/v1/models",
        "nexa": "http://localhost:18181/v1/models",
    }
    def _probe(key: str, url: str) -> tuple[str, bool]:
        try:
            resp = httpx.get(url, timeout=1.0)
            return key, resp.status_code < 500
        except Exception:
            return key, False

    detected: set[str] = set()
    with ThreadPoolExecutor(max_workers=len(_PROBES)) as pool:
        futures = [pool.submit(_probe, key, url) for key, url in _PROBES.items()]
        for future in as_completed(futures):
            key, available = future.result()
            if available:
                detected.add(key)
    # Preserve the stable display/preference order regardless of completion order.
    return [key for key in _PROBES if key in detected]


def _next_steps_text(engine: str, model: str = "", platform: str = "") -> str:
    """Return engine-specific next-steps guidance after init."""
    pull_model = model or "qwen3.5:2b"
    ask_cmd = _BRAND.command_name
    is_windows = platform.lower() == "windows"
    ollama_install = (
        "     Download Ollama for Windows from https://ollama.com/download\n"
        "     Start Ollama from the Start menu or run: ollama serve\n"
        if is_windows
        else "     curl -fsSL https://ollama.com/install.sh | sh\n"
        "     ollama serve\n"
    )
    llamacpp_install = (
        "     Install llama.cpp for Windows and run:\n"
        "     llama-server -m path\\to\\model.gguf\n"
        if is_windows
        else "     brew install llama.cpp\n"
        "     llama-server -m path/to/model.gguf\n"
    )
    steps: dict[str, str] = {
        "ollama": (
            "Next steps:\n"
            "\n"
            "  1. Install and start Ollama:\n"
            + ollama_install
            + "\n"
            + "  2. Pull a model:\n"
            + f"     ollama pull {pull_model}\n"
            + "\n"
            + "  3. Try it out:\n"
            + f'     {ask_cmd} ask "Hello"\n'
            + "\n"
            + f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "vllm": (
            "Next steps:\n"
            "\n"
            "  1. Install and start vLLM:\n"
            "     pip install vllm\n"
            "     vllm serve Qwen/Qwen3-4B\n"
            "\n"
            "  2. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n'
            "\n"
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "llamacpp": (
            "Next steps:\n"
            "\n"
            "  1. Install and start llama.cpp:\n"
            + llamacpp_install
            + "\n"
            + "  2. Try it out:\n"
            + f'     {ask_cmd} ask "Hello"\n'
            + "\n"
            + f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "sglang": (
            "Next steps:\n"
            "\n"
            "  1. Install and start SGLang:\n"
            "     pip install sglang[all]\n"
            "     python -m sglang.launch_server --model-path Qwen/Qwen3-8B\n"
            "\n"
            "  2. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n'
            "\n"
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "mlx": (
            "Next steps:\n"
            "\n"
            "  1. Install and start MLX:\n"
            "     pip install mlx-lm\n"
            "     mlx_lm.server --model mlx-community/Qwen2.5-7B-4bit\n"
            "\n"
            "  2. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n'
            "\n"
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "lmstudio": (
            "Next steps:\n"
            "\n"
            "  1. Download LM Studio:\n"
            "     https://lmstudio.ai\n"
            "\n"
            "  2. Load a model and start the local server (port 1234)\n"
            "\n"
            "  3. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n'
            "\n"
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "exo": (
            "Next steps:\n\n"
            "  1. Install and start Exo:\n"
            "     pip install exo\n"
            "     exo\n\n"
            "  2. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n\n'
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "nexa": (
            "Next steps:\n\n"
            "  1. Install and start Nexa:\n"
            "     pip install nexaai\n"
            "     nexa server\n\n"
            "  2. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n\n'
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
        "lemonade": (
            "Next steps:\n\n"
            "  1. Install Lemonade for your platform:\n"
            "     https://lemonade-server.ai/\n\n"
            "  2. Start the Lemonade server\n\n"
            "  3. Try it out:\n"
            f'     {ask_cmd} ask "Hello"\n\n'
            f"  Run `{ask_cmd} doctor` to verify your setup."
        ),
    }
    return steps.get(engine, steps["ollama"])


def _quick_privacy_check(console: Console) -> None:
    """Run critical privacy checks and print compact summary."""
    scanner = PrivacyScanner()
    results = scanner.run_quick()
    if results:
        console.print("  [bold]Privacy check:[/bold]")
        for r in results:
            if r.status == "ok":
                console.print(f"  [green]\u2713[/green] {r.message}")
            elif r.status == "warn":
                console.print(f"  [yellow]![/yellow] {r.message}")
            elif r.status == "fail":
                console.print(f"  [red]\u2717[/red] {r.message}")
    console.print()
    console.print(
        f"  Run [cyan]{_BRAND.command_name} scan[/cyan] for a full environment audit."
    )


def _do_download(engine: str, model: str, spec, console: Console) -> None:
    """Dispatch model download based on engine type."""
    import os

    if engine == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        ollama_pull(host, model, console)
    elif engine == "llamacpp":
        repo = spec.metadata.get("hf_repo", "")
        gguf = spec.metadata.get("gguf_file", "")
        if repo and gguf:
            console.print(f"  Downloading [cyan]{gguf}[/cyan] from {repo}...")
            hf_download(repo, gguf, console)
        else:
            console.print(f"  [yellow]No GGUF download info for {model}[/yellow]")
    elif engine == "mlx":
        mlx_repo = spec.metadata.get("mlx_repo", "")
        if mlx_repo:
            console.print(f"  Downloading [cyan]{mlx_repo}[/cyan]...")
            hf_download(mlx_repo, None, console)
        else:
            console.print(f"  [yellow]No MLX repo info for {model}[/yellow]")
    elif engine in ("vllm", "sglang"):
        console.print(
            f"  [cyan]{model}[/cyan] will download automatically when "
            f"{engine} starts serving it."
        )
    else:
        console.print(f"  Download {model} through the {engine} interface.")


def _model_that_fits_disk(hw, engine: str, config_dir: Path) -> str:
    """Return the largest hardware-compatible model that safely fits disk."""
    from bujji.intelligence.model_catalog import BUILTIN_MODELS

    recommended = recommend_model(hw, engine)
    if not recommended:
        return ""
    root = config_dir
    while not root.exists() and root != root.parent:
        root = root.parent
    free_gb = shutil.disk_usage(root).free / (1024**3)
    safety_gb = 2.0
    recommended_spec = find_model_spec(recommended)
    max_parameters = (
        recommended_spec.parameter_count_b if recommended_spec else float("inf")
    )
    candidates = [
        spec
        for spec in BUILTIN_MODELS
        if engine in spec.supported_engines
        and spec.provider == "alibaba"
        and spec.model_id.startswith("qwen3.5:")
        and spec.parameter_count_b > 0
        and spec.parameter_count_b <= max_parameters
    ]
    candidates.sort(key=lambda spec: spec.parameter_count_b, reverse=True)
    for spec in candidates:
        if (
            estimated_download_gb(spec.parameter_count_b, spec.model_id) + safety_gb
            <= free_gb
        ):
            return spec.model_id
    return ""


@click.command()
@click.option(
    "--force", is_flag=True, help="Overwrite existing config without prompting."
)
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to config file to use.",
)
@click.option(
    "--full",
    "full_config",
    is_flag=True,
    help="Generate full reference config with all sections",
)
@click.option(
    "--engine",
    type=click.Choice(_SUPPORTED_ENGINES, case_sensitive=False),
    default=None,
    help="Inference engine to use (skips interactive selection).",
)
@click.option(
    "--no-download", is_flag=True, default=False, help="Skip the model download prompt."
)
@click.option(
    "--auto-download",
    is_flag=True,
    default=False,
    hidden=True,
    help="Download the recommended model without prompting (installer use).",
)
@click.option(
    "--no-scan",
    "skip_scan",
    is_flag=True,
    default=False,
    help="Skip the post-init security environment audit.",
)
@click.option(
    "--host",
    default=None,
    help="Remote engine host URL (e.g. http://192.168.1.50:11434).",
)
@click.option(
    "--digest",
    "enable_digest",
    is_flag=True,
    default=False,
    help="Include Morning Digest config section.",
)
@click.option(
    "--preset",
    type=click.Choice(
        [
            "morning-digest-mac",
            "morning-digest-linux",
            "morning-digest-minimal",
            "deep-research",
            "code-assistant",
            "scheduled-monitor",
            "chat-simple",
        ],
        case_sensitive=False,
    ),
    default=None,
    help="Use a pre-built starter config instead of generating one.",
)
@click.option(
    "--from-bare-bujji",
    is_flag=True,
    default=False,
    hidden=True,
    help="Run init non-interactively; called by the bare-bujji first-run guard.",
)
@click.pass_context
def init(
    ctx: click.Context,
    force: bool,
    config: Optional[Path],
    full_config: bool = False,
    engine: Optional[str] = None,
    no_download: bool = False,
    auto_download: bool = False,
    skip_scan: bool = False,
    host: Optional[str] = None,
    enable_digest: bool = False,
    preset: Optional[str] = None,
    from_bare_bujji: bool = False,
) -> None:
    """Detect hardware and generate ~/.bujji/config.toml."""
    print_banner(quiet=(ctx.obj or {}).get("quiet", False))
    console = Console()

    # Cloud auto-detect â€” inform user if a key is in env.
    detected_cloud = detect_cloud_keys()
    if detected_cloud is not None:
        console.print(
            f"[cyan]Detected cloud key in env:[/cyan] {detected_cloud.env_var} "
            f"(provider: {detected_cloud.provider}). "
            f"Cloud inference is available via this key."
        )

    if DEFAULT_CONFIG_PATH.exists() and not force:
        console.print(
            f"[yellow]Config already exists at {DEFAULT_CONFIG_PATH}[/yellow]"
        )
        console.print("Use [bold]--force[/bold] to overwrite.")
        raise SystemExit(1)

    # Handle --preset: copy a starter config and return early
    if preset:
        examples_dir = (
            Path(__file__).resolve().parents[2] / "configs" / "bujji" / "examples"
        )
        # Also check installed package location
        if not examples_dir.exists():
            examples_dir = (
                Path(__file__).resolve().parents[3]
                / "configs"
                / "bujji"
                / "examples"
            )
        preset_path = examples_dir / f"{preset}.toml"
        if not preset_path.exists():
            console.print(f"[red]Preset '{preset}' not found.[/red]")
            console.print(f"  Looked in: {examples_dir}")
            raise SystemExit(1)
        DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_CONFIG_PATH.write_text(preset_path.read_text())
        console.print(
            f"[green]Preset '{preset}' installed to {DEFAULT_CONFIG_PATH}[/green]"
        )
        console.print(
            "\n  Edit the config to customize, then run "
            f"[bold]{_BRAND.command_name} doctor[/bold] to verify."
        )
        return

    console.print("[bold]Detecting hardware...[/bold]")
    hw = detect_hardware()

    console.print(f"  Platform : {hw.platform}")
    console.print(f"  CPU      : {hw.cpu_brand} ({hw.cpu_count} cores)")
    console.print(f"  RAM      : {hw.ram_gb} GB")
    if hw.gpu:
        mem_label = "unified memory" if hw.gpu.vendor == "apple" else "VRAM"
        gpu = hw.gpu
        console.print(
            f"  GPU      : {gpu.name} ({gpu.vram_gb} GB {mem_label}, x{gpu.count})"
        )
    else:
        console.print("  GPU      : none detected")

    # Explicit engine selection needs no network discovery. This keeps scripted
    # setup and tests fast while auto/interactive setup still detects Ollama.
    running = _detect_running_engines() if engine is None else []

    # Resolve engine: explicit flag > interactive selection > auto-detect
    if engine is None and config is None:
        recommended = recommend_engine(hw)
        # Bare-bujji cold path: use the recommended engine non-interactively.
        if from_bare_bujji or auto_download:
            engine = "ollama" if "ollama" in running else recommended
        else:
            console.print()
            console.print("[bold]Detecting running inference engines...[/bold]")
            if running:
                console.print(f"  Found running: [green]{', '.join(running)}[/green]")
            else:
                console.print("  No running engines detected.")

            # Build choices: show running engines first, then recommended, then rest
            seen: set[str] = set()
            choices: list[str] = []
            for r in running:
                if r not in seen:
                    choices.append(r)
                    seen.add(r)
            if recommended not in seen:
                choices.append(recommended)
                seen.add(recommended)
            for e in _SUPPORTED_ENGINES:
                if e not in seen:
                    choices.append(e)
                    seen.add(e)

            # Default: first running engine, or hardware recommendation
            default = running[0] if running else recommended

            labels = []
            for c in choices:
                parts = [c]
                if c in running:
                    parts.append("running")
                if c == recommended:
                    parts.append("recommended")
                labels.append(
                    f"  {c}" + (f"  ({', '.join(parts[1:])})" if len(parts) > 1 else "")
                )

            console.print()
            console.print("[bold]Available engines:[/bold]")
            for label in labels:
                console.print(label)

            engine = click.prompt(
                "\nSelect inference engine",
                type=click.Choice(choices, case_sensitive=False),
                default=default,
            )

    # Probe remote host if specified
    if host:
        console.print("\n[bold]Checking remote host...[/bold]")
        try:
            resp = httpx.get(host.rstrip("/") + "/", timeout=2.0)
            if resp.status_code < 500:
                console.print(f"  [green]Reachable[/green] ({host})")
            else:
                console.print(
                    f"  [yellow]Warning:[/yellow] Host returned status "
                    f"{resp.status_code} â€” writing config anyway."
                )
        except Exception:
            console.print(
                f"  [yellow]Warning:[/yellow] Host unreachable ({host}) "
                f"â€” writing config anyway."
            )

    selected_engine = engine or (
        "ollama" if "ollama" in running else recommend_engine(hw)
    )
    selected_model = _model_that_fits_disk(hw, selected_engine, DEFAULT_CONFIG_DIR)

    if config:
        toml_content = config.read_text()
    else:
        if full_config:
            toml_content = generate_default_toml(
                hw, engine=selected_engine, host=host, model=selected_model
            )
        else:
            toml_content = generate_minimal_toml(
                hw, engine=selected_engine, host=host, model=selected_model
            )

    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if config:
        config.write_text(toml_content)
    else:
        DEFAULT_CONFIG_PATH.write_text(toml_content)

    console.print()
    console.print(
        Panel(
            escape(toml_content),
            title=str(DEFAULT_CONFIG_PATH),
            border_style="green",
        )
    )
    # Append Morning Digest section if requested
    if enable_digest:
        digest_section = """
# â”€â”€â”€ Morning Digest â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
[digest]
enabled = true
schedule = "0 7 * * *"
timezone = "America/Los_Angeles"
persona = "bujji"
honorific = "sir"
tts_backend = "cartesia"
voice_id = "c8f7835e-28a3-4f0c-80d7-c1302ac62aae"
voice_speed = 1.2
sections = ["health", "messages", "calendar", "world"]

[digest.health]
sources = ["oura"]

[digest.messages]
sources = ["gmail", "google_tasks", "imessage"]

[digest.calendar]
sources = ["gcalendar"]

[digest.world]
sources = ["hackernews", "news_rss"]
"""
        target = config if config else DEFAULT_CONFIG_PATH
        existing = target.read_text()
        target.write_text(existing + digest_section)
        toml_content = target.read_text()
        console.print(
            "[green]Morning Digest config added.[/green] "
            f"Run [bold]{_BRAND.command_name} connect gdrive[/bold] to connect "
            f"Google services, then [bold]{_BRAND.command_name} digest --fresh[/bold]."
        )

    console.print("[green]Config written successfully.[/green]")

    # Create default memory files (skip if they already exist)
    soul_path = DEFAULT_CONFIG_DIR / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(
            "# Agent Persona\n\nYou are Bujji, a helpful local AI assistant.\n"
        )

    memory_path = DEFAULT_CONFIG_DIR / "MEMORY.md"
    if not memory_path.exists():
        memory_path.write_text("# Agent Memory\n\n")

    user_path = DEFAULT_CONFIG_DIR / "USER.md"
    if not user_path.exists():
        user_path.write_text("# User Profile\n\n")

    skills_dir = DEFAULT_CONFIG_DIR / "skills"
    skills_dir.mkdir(exist_ok=True)

    model = selected_model

    if not model:
        console.print(
            "\n  [yellow]! Not enough memory to run any local model.[/yellow]\n"
            "  Consider a cloud engine or a machine with more RAM."
        )
    else:
        spec = find_model_spec(model)
        size_gb = (
            estimated_download_gb(spec.parameter_count_b, spec.model_id) if spec else 0
        )
        avail = _available_memory_gb(hw)
        console.print(
            f"\n  [bold]Recommended model:[/bold] {model} (~{size_gb:.1f} GB)"
            f"  [dim](selected for {avail:.0f} GB available memory)[/dim]"
        )

        if auto_download and spec:
            _do_download(selected_engine, model, spec, console)
        elif not no_download and not from_bare_bujji and spec:
            prompt = f"  Download {model} (~{size_gb:.1f} GB) now?"
            if click.confirm(prompt, default=True):
                _do_download(selected_engine, model, spec, console)
            else:
                console.print(
                    f"\n  Skipped. Download later with:\n"
                    f"    [bold]{_BRAND.command_name} model pull {model}[/bold]"
                )

    if not skip_scan:
        _quick_privacy_check(console)
    console.print()
    console.print(
        Panel(
            _next_steps_text(selected_engine, model, hw.platform),
            title="Getting Started",
            border_style="cyan",
        )
    )
