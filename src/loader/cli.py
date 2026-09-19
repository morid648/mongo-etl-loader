"""CLI entrypoint: thin wrapper around loader.pipeline (PRD §6, §7.8)."""

from __future__ import annotations

import json

import typer
from dotenv import load_dotenv

from loader.config import ConfigError, load_config
from loader.logging_utils import configure_logging

app = typer.Typer(add_completion=False)


@app.callback()
def _main() -> None:
    """MongoDB Database Load Process CLI."""


@app.command()
def run(
    source: str = typer.Option(..., "--source", help="Source type: csv or sql"),
    config: str = typer.Option(..., "--config", help="Path to the pipeline YAML config"),
) -> None:
    """Run one load: extract from SOURCE per CONFIG, transform, load into MongoDB."""
    load_dotenv()
    configure_logging()

    try:
        pipeline_config = load_config(config)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if source == "csv":
        from loader.pipeline import run_csv_pipeline

        summary = run_csv_pipeline(pipeline_config)
    elif source == "sql":
        from loader.pipeline import run_sql_pipeline

        summary = run_sql_pipeline(pipeline_config)
    else:
        typer.echo(f"unknown source type: {source}", err=True)
        raise typer.Exit(code=1)

    typer.echo(summary.as_json())


@app.command()
def status(
    last: int = typer.Option(10, "--last", help="Number of recent runs to show"),
) -> None:
    """Show recent run summaries from the `_pipeline_runs` MongoDB collection."""
    load_dotenv()
    configure_logging()

    from loader.mongo_loader import get_client, get_database, get_recent_runs

    client = get_client()
    try:
        db = get_database(client)
        runs = get_recent_runs(db, last=last)
    finally:
        client.close()

    if not runs:
        typer.echo("no run history found")
        return

    for run_doc in runs:
        run_doc.pop("_id", None)
        typer.echo(json.dumps(run_doc, indent=2, default=str))


@app.command()
def validate(
    config: str = typer.Option(..., "--config", help="Path to the pipeline YAML config"),
) -> None:
    """Dry-run CONFIG: report what would load without writing to MongoDB."""
    load_dotenv()
    configure_logging()

    try:
        pipeline_config = load_config(config)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    from loader.pipeline import validate_pipeline

    summary = validate_pipeline(pipeline_config)
    typer.echo(summary.as_json())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
