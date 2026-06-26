from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from .collectors import Collector
from .config import load_environment, load_sources
from .latest_selection import default_latest_source_ids, latest_quality_report, select_latest_articles
from .llm import enrich_with_openai, evaluate_with_openai
from .models import CollectionOptions
from .ranking import build_quality_report, rank_articles
from .render import make_output_dir, write_package
from .topic_radar import build_issue_radar

app = typer.Typer(help="Weekly AI newsletter crawler and HTML packager.")
console = Console()


@app.command()
def build(
    sources: Path = typer.Option(Path("config/sources.yaml"), help="Source configuration YAML."),
    output: Path = typer.Option(Path("outputs"), help="Output directory."),
    env_file: Path | None = typer.Option(None, help="Optional .env file path."),
    days: int = typer.Option(7, min=1, max=31, help="Collection window in days."),
    limit: int = typer.Option(10, min=1, max=30, help="Number of main articles."),
    use_llm: bool = typer.Option(True, help="Use OpenAI for Korean editing and quality evaluation."),
    selection_mode: Literal["issue", "rank", "latest"] = typer.Option(
        "issue",
        help="Article selection mode: issue radar, legacy ranking, or latest dated articles from listed sites.",
    ),
    issue_radar: bool = typer.Option(True, help="Select articles through issue radar first."),
    latest_source_ids: str = typer.Option(
        "",
        help="Comma-separated source ids for --selection-mode latest. Defaults to enabled rss/webpage sources.",
    ),
    latest_fill: bool = typer.Option(
        True,
        help="When latest selected source ids produce fewer than limit, fill from other enabled rss/webpage sources.",
    ),
    require_dates: bool = typer.Option(True, help="Drop items when no date can be parsed."),
    strict_week: bool = typer.Option(True, help="Drop items outside the collection window."),
    per_source_limit: int = typer.Option(20, min=1, max=100, help="Maximum candidate items per source."),
) -> None:
    load_environment(env_file)
    source_list = load_sources(sources)
    if selection_mode == "latest":
        require_dates = True
        strict_week = True
        issue_radar = False
    collector = Collector(
        options=CollectionOptions(
            require_dates=require_dates,
            strict_week=strict_week,
            per_source_limit=per_source_limit,
        )
    )
    candidates = []
    try:
        for source in source_list.sources:
            if not source.enabled:
                continue
            try:
                rows = collector.collect(source, days)
                candidates.extend(rows)
                console.print(f"[green]OK[/green] {source.name}: {len(rows)}")
            except Exception as exc:
                console.print(f"[yellow]WARN[/yellow] {source.name}: {exc}")
    finally:
        collector.close()

    issues = []
    if selection_mode == "latest":
        source_ids = _parse_source_ids(latest_source_ids) or default_latest_source_ids(source_list.sources)
        fallback_source_ids = (
            default_latest_source_ids(source_list.sources) - source_ids if latest_fill else set()
        )
        selected = select_latest_articles(
            candidates,
            source_list.sources,
            days=days,
            limit=limit,
            source_ids=source_ids,
            fallback_source_ids=fallback_source_ids,
        )
        report = latest_quality_report(
            selected,
            candidates,
            source_ids=source_ids,
            fallback_source_ids=fallback_source_ids,
            days=days,
        )
    elif selection_mode == "rank" or not issue_radar:
        selected = rank_articles(candidates, limit=limit)
        report = build_quality_report(selected, candidates)
    else:
        issues, selected = build_issue_radar(candidates, limit=4, use_llm=use_llm)
        report = build_quality_report(selected, candidates)
    if use_llm:
        selected = enrich_with_openai(selected)
    report["issues"] = [issue.model_dump(mode="json") for issue in issues]
    if use_llm:
        report = evaluate_with_openai(selected, report)

    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=days)
    output_dir = make_output_dir(output, period_end)
    package = write_package(output_dir, period_start, period_end, candidates, selected, report, issues=issues)
    console.print(f"[bold green]Created[/bold green] {package.output_dir / 'newsletter.html'}")
    console.print(f"[bold green]Created[/bold green] {package.output_dir.with_suffix('.zip')}")


def _parse_source_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


@app.command()
def sample(output: Path = typer.Option(Path("outputs"), help="Output directory.")) -> None:
    from .sample_data import sample_articles

    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=7)
    candidates = sample_articles()
    selected = rank_articles(candidates, limit=10)
    report = build_quality_report(selected, candidates)
    report["llm_evaluation"] = {
        "score": 82,
        "comment": "샘플 데이터 기준입니다. 실제 사이트 수집 후 중복성과 내부 업무 관련성을 재평가하세요.",
    }
    output_dir = make_output_dir(output, period_end)
    write_package(output_dir, period_start, period_end, candidates, selected, report)
    console.print(f"[bold green]Created sample package[/bold green] {output_dir}")


if __name__ == "__main__":
    app()
