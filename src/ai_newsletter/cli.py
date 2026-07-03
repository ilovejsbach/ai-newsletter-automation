from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from .collectors import Collector
from .config import load_environment, load_sources
from .editorial_selection import select_editorial_articles
from .latest_selection import default_latest_source_ids, latest_quality_report, select_latest_articles
from .llm import enrich_with_openai, evaluate_with_openai
from .models import CollectionOptions
from .ranking import build_quality_report, rank_articles
from .render import make_output_dir, write_package
from .topic_radar import build_issue_radar

app = typer.Typer(help="Weekly AI newsletter crawler and HTML packager.")
console = Console()

# (key, one-line Korean description) for the interactive menu and help.
MODE_CHOICES: list[tuple[str, str]] = [
    ("issue", "이슈 레이더 — 주제로 묶어 선별 (기본)"),
    ("latest", "최신 사이트 — 지정 사이트의 최근 1주 기사"),
    ("editorial", "편집자 — LLM 뉴스가치 채점 + 주제 중복제거"),
    ("editorial-diverse", "편집자+다양성 — 파운데이션 모델 쏠림 완화"),
    ("rank", "레거시 랭킹 — 순수 점수 정렬"),
]


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """AI 주간 뉴스레터 생성기.

    하위 명령 없이 실행하면 대화형 모드로 진입합니다. 플래그로 직접 지정하려면
    'build'를, 샘플은 'sample'을 사용하세요.
    """
    if ctx.invoked_subcommand is None:
        _interactive()


@app.command()
def build(
    sources: Path = typer.Option(Path("config/sources.yaml"), help="Source configuration YAML."),
    output: Path = typer.Option(Path("outputs"), help="Output directory."),
    env_file: Path | None = typer.Option(None, help="Optional .env file path."),
    days: int = typer.Option(7, min=1, max=31, help="Collection window in days."),
    limit: int = typer.Option(10, min=1, max=30, help="Number of main articles."),
    use_llm: bool = typer.Option(True, help="Use OpenAI for Korean editing and quality evaluation."),
    selection_mode: Literal["issue", "rank", "latest", "editorial", "editorial-diverse"] = typer.Option(
        "issue",
        help="Article selection mode: issue radar, legacy ranking, latest dated articles, "
        "editorial (LLM newsworthiness + topic dedup), or editorial-diverse (adds "
        "category/vendor diversity so it is not all foundation-model news).",
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
    """Collect, select, and render the weekly newsletter with explicit flags."""
    _run_build(
        sources=sources,
        output=output,
        env_file=env_file,
        days=days,
        limit=limit,
        use_llm=use_llm,
        selection_mode=selection_mode,
        issue_radar=issue_radar,
        latest_source_ids=latest_source_ids,
        latest_fill=latest_fill,
        require_dates=require_dates,
        strict_week=strict_week,
        per_source_limit=per_source_limit,
    )


def _run_build(
    *,
    sources: Path,
    output: Path,
    env_file: Path | None,
    days: int,
    limit: int,
    use_llm: bool,
    selection_mode: str,
    issue_radar: bool,
    latest_source_ids: str,
    latest_fill: bool,
    require_dates: bool,
    strict_week: bool,
    per_source_limit: int,
) -> None:
    load_environment(env_file)
    source_list = load_sources(sources)
    if selection_mode == "latest":
        require_dates = True
        strict_week = True
        issue_radar = False
    if selection_mode in ("editorial", "editorial-diverse"):
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
    elif selection_mode in ("editorial", "editorial-diverse"):
        selected, report = select_editorial_articles(
            candidates,
            limit=limit,
            use_llm=use_llm,
            diversify=(selection_mode == "editorial-diverse"),
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


def _interactive() -> None:
    """대화형으로 옵션을 골라 주간 뉴스레터를 생성합니다 (플래그를 외울 필요 없음)."""
    import os

    from rich.prompt import Confirm, IntPrompt, Prompt

    console.print("[bold cyan]AI 뉴스레터 — 대화형 생성[/bold cyan]")
    console.print("[dim]Enter를 누르면 대괄호 안의 기본값이 사용됩니다.[/dim]\n")

    console.print("선별 모드를 고르세요:")
    for i, (key, desc) in enumerate(MODE_CHOICES, 1):
        console.print(f"  [bold]{i}[/bold]. {desc}")
    picked = Prompt.ask(
        "번호",
        choices=[str(i) for i in range(1, len(MODE_CHOICES) + 1)],
        default="1",
        show_choices=False,
    )
    selection_mode = MODE_CHOICES[int(picked) - 1][0]

    days = _clamp(IntPrompt.ask("수집 기간(일)", default=7), 1, 31)
    limit = _clamp(IntPrompt.ask("메인 기사 수", default=10), 1, 30)

    use_llm = Confirm.ask("OpenAI로 한국어 편집(LLM)을 사용할까요?", default=True)
    if use_llm and not os.getenv("OPENAI_API_KEY"):
        load_environment(None)  # pick up .env before warning
        if not os.getenv("OPENAI_API_KEY"):
            console.print(
                "[yellow]주의: OPENAI_API_KEY가 없습니다. editorial 계열은 휴리스틱으로 대체되고, "
                "그 외 모드는 한국어 편집 없이 생성됩니다.[/yellow]"
            )

    latest_source_ids = ""
    latest_fill = True
    if selection_mode == "latest":
        latest_source_ids = Prompt.ask(
            "지정 사이트 id (쉼표로 구분, 비우면 전체 rss/webpage)", default=""
        )
        latest_fill = Confirm.ask("지정 사이트에서 부족하면 다른 사이트로 보강할까요?", default=True)

    output = Prompt.ask("출력 폴더", default="outputs")

    console.print("\n[bold]설정 요약[/bold]")
    console.print(
        f"  모드=[cyan]{selection_mode}[/cyan]  기간={days}일  기사수={limit}  "
        f"LLM={'예' if use_llm else '아니오'}  출력={output}"
    )
    if selection_mode == "latest" and latest_source_ids:
        console.print(f"  지정 사이트={latest_source_ids}  보강={'예' if latest_fill else '아니오'}")
    if not Confirm.ask("이 설정으로 생성할까요?", default=True):
        console.print("[yellow]취소되었습니다.[/yellow]")
        raise typer.Exit()

    console.print()
    _run_build(
        sources=Path("config/sources.yaml"),
        output=Path(output),
        env_file=None,
        days=days,
        limit=limit,
        use_llm=use_llm,
        selection_mode=selection_mode,
        issue_radar=True,
        latest_source_ids=latest_source_ids,
        latest_fill=latest_fill,
        require_dates=True,
        strict_week=True,
        per_source_limit=20,
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


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
