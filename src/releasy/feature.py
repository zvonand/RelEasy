"""Feature management: add, enable, disable, remove, list."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from releasy.config import Config, FeatureConfig, save_config

console = Console()


def add_feature(
    config: Config,
    feature_id: str,
    branch: str,
    description: str,
    enabled: bool = True,
) -> bool:
    """Add a new feature to the configuration."""
    if config.get_feature(feature_id):
        console.print(f"[red]Feature '{feature_id}' already exists[/red]")
        return False

    config.features.append(
        FeatureConfig(
            id=feature_id,
            description=description,
            branch=branch,
            enabled=enabled,
        )
    )
    save_config(config)
    console.print(f"[green]✓[/green] Added feature [cyan]{feature_id}[/cyan] ({branch})")
    return True


def enable_feature(config: Config, feature_id: str) -> bool:
    """Enable a feature."""
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    if feat.enabled:
        console.print(f"[yellow]Feature '{feature_id}' is already enabled[/yellow]")
        return True
    feat.enabled = True
    save_config(config)
    console.print(f"[green]✓[/green] Enabled feature [cyan]{feature_id}[/cyan]")
    return True


def disable_feature(config: Config, feature_id: str) -> bool:
    """Disable a feature."""
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    if not feat.enabled:
        console.print(f"[yellow]Feature '{feature_id}' is already disabled[/yellow]")
        return True
    feat.enabled = False
    save_config(config)
    console.print(f"[green]✓[/green] Disabled feature [cyan]{feature_id}[/cyan]")
    return True


def remove_feature(config: Config, feature_id: str) -> bool:
    """Remove a feature from the configuration."""
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    config.features = [f for f in config.features if f.id != feature_id]
    save_config(config)
    console.print(f"[green]✓[/green] Removed feature [cyan]{feature_id}[/cyan]")
    return True


def list_features(config: Config) -> None:
    """List all features in a table."""
    table = Table(title="Features")
    table.add_column("ID", style="cyan")
    table.add_column("Branch")
    table.add_column("Description")
    table.add_column("Enabled")

    for feat in config.features:
        enabled_str = "[green]yes[/green]" if feat.enabled else "[dim]no[/dim]"
        table.add_row(feat.id, feat.branch, feat.description, enabled_str)

    if not config.features:
        console.print("[dim]No features configured.[/dim]")
    else:
        console.print(table)
