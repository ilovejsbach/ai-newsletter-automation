from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from .collectors import Collector
from .config import load_environment, load_sources
from .editorial_selection import select_consensus_articles, select_editorial_articles
from .latest_selection import default_latest_source_ids, latest_quality_report, select_latest_articles
from .llm import (
    enrich_with_openai,
    evaluate_with_openai,
    generate_weekly_overview,
    grounding_flags,
)
from .models import CollectionOptions
from .ranking import build_quality_report, rank_articles
from .render import make_output_dir, write_package
from .sections import select_sectioned_articles
from .topic_radar import build_issue_radar
from .usage import usage

app = typer.Typer(help="Weekly AI newsletter crawler and HTML packager.")
console = Console()

# (key, one-line Korean description) for the interactive menu and help.
MODE_CHOICES: list[tuple[str, str]] = [
    ("sectioned", "섹션 구성 — 섹션 최소보장 + 4단 본문 + 콜아웃 (기본)"),
    ("issue", "이슈 레이더 — 주제로 묶어 선별"),
    ("latest", "최신 사이트 — 지정 사이트의 최근 1주 기사"),
    ("editorial", "편집자 — LLM 뉴스가치 채점 + 주제 중복제거"),
    ("editorial-diverse", "편집자+다양성 — 파운데이션 모델 쏠림 완화"),
    ("consensus", "중복도 — 여러 출처가 함께 다룬 화제 우선"),
    ("rank", "레거시 랭킹 — 순수 점수 정렬"),
]


def _enable_os_trust_store() -> None:
    """Make Python's TLS use the OS certificate store so corporate/self-signed
    root CAs (e.g. an SSL-inspecting proxy) are trusted. This survives `uv sync`
    reinstalling certifi, unlike patching the certifi bundle by hand. No-op if
    truststore is unavailable (e.g. a minimal install) — falls back to certifi.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """AI 주간 뉴스레터 생성기.

    하위 명령 없이 실행하면 대화형 모드로 진입합니다. 플래그로 직접 지정하려면
    'build'를, 샘플은 'sample'을 사용하세요.
    """
    _enable_os_trust_store()
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
    selection_mode: Literal[
        "issue", "rank", "latest", "editorial", "editorial-diverse", "consensus", "sectioned"
    ] = typer.Option(
        "sectioned",
        help="Article selection mode (기본: sectioned — 섹션 최소보장 + 4단 본문). "
        "그 외: issue radar, legacy ranking, latest dated articles, "
        "editorial (LLM newsworthiness + topic dedup), editorial-diverse, "
        "consensus (rank by how many sources cover the story).",
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
    include_candidates: bool = typer.Option(
        False, help="Also load config/sources.candidate.yaml (tagged as the 'candidate' set)."
    ),
    candidate_sources: Path = typer.Option(
        Path("config/sources.candidate.yaml"), help="Candidate source YAML for --include-candidates."
    ),
    include_social: bool = typer.Option(
        True,
        help="Also collect social-signal sources (config/sources.social.yaml). "
        "They are never published; they boost topics and feed related_coverage (sectioned mode). "
        "--no-include-social로 끌 수 있음.",
    ),
    social_sources: Path = typer.Option(
        Path("config/sources.social.yaml"), help="Social signal source YAML for --include-social."
    ),
    capture: bool = typer.Option(
        True, help="PNG 캡처·게시 패키지 생성 여부. --no-capture면 HTML까지만 (나중에 `capture` 명령으로 보완)."
    ),
    theme: Literal["classic", "editorial", "magazine", "report"] = typer.Option(
        "editorial",
        help="디자인 테마: classic(기존), editorial(시안A 미니멀), magazine(시안B 카드), report(시안C 네이비/골드). 폰트는 전부 Pretendard.",
    ),
    thumbs: bool = typer.Option(
        True, help="본지 기사 행에 대표 이미지 썸네일 표시 (--no-thumbs로 텍스트 전용)."
    ),
    rubric: Literal["standard", "sota"] = typer.Option(
        "standard",
        help="채점 루브릭: standard(기존) | sota(프론티어 SOTA 출시·중단·재배포 라이프사이클 가중). 비교 실험용.",
    ),
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
        include_candidates=include_candidates,
        candidate_sources=candidate_sources,
        include_social=include_social,
        social_sources=social_sources,
        capture=capture,
        theme=theme,
        thumbs=thumbs,
        rubric=rubric,
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
    include_candidates: bool = False,
    candidate_sources: Path = Path("config/sources.candidate.yaml"),
    include_social: bool = False,
    social_sources: Path = Path("config/sources.social.yaml"),
    capture: bool = True,
    theme: str = "editorial",
    thumbs: bool = True,
    rubric: str = "standard",
) -> None:
    usage.reset()
    t0 = time.monotonic()
    load_environment(env_file)
    source_list = load_sources(sources)
    all_sources = list(source_list.sources)
    if include_candidates and candidate_sources and candidate_sources.exists():
        candidate_list = load_sources(candidate_sources)
        existing_ids = {s.id for s in all_sources}
        added = 0
        for s in candidate_list.sources:
            s.source_set = "candidate"
            if s.id not in existing_ids:
                all_sources.append(s)
                existing_ids.add(s.id)
                added += 1
        console.print(f"[cyan]후보 소스 {added}개 포함 (candidate set)[/cyan]")
    if include_social and social_sources and social_sources.exists():
        social_list = load_sources(social_sources)
        existing_ids = {s.id for s in all_sources}
        added = 0
        for s in social_list.sources:
            s.panel = "social"
            s.source_set = "social"
            if s.id not in existing_ids:
                all_sources.append(s)
                existing_ids.add(s.id)
                added += 1
        console.print(f"[cyan]소셜 신호 소스 {added}개 포함 (게시 제외, 부스팅 전용)[/cyan]")
    source_sets = {s.id: s.source_set for s in all_sources}
    if selection_mode == "latest":
        require_dates = True
        strict_week = True
        issue_radar = False
    if selection_mode in ("editorial", "editorial-diverse", "consensus", "sectioned"):
        issue_radar = False
    collector = Collector(
        options=CollectionOptions(
            require_dates=require_dates,
            strict_week=strict_week,
            per_source_limit=per_source_limit,
        )
    )
    candidates = []
    enabled_sources = [s for s in all_sources if s.enabled]

    def _collect_one(source):
        try:
            return source, collector.collect(source, days), None
        except Exception as exc:  # noqa: BLE001 - reported per source below
            return source, [], exc

    try:
        # Sources are collected concurrently; the shared httpx.Client is thread-safe
        # and results are aggregated on this thread, so no locking is needed.
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(_collect_one, s) for s in enabled_sources]
            for future in as_completed(futures):
                source, rows, exc = future.result()
                if exc is not None:
                    console.print(f"[yellow]WARN[/yellow] {source.name}: {exc}")
                else:
                    candidates.extend(rows)
                    console.print(f"[green]OK[/green] {source.name}: {len(rows)}")
    finally:
        collector.close()
    t_collect = time.monotonic()

    # Social posts are signal-only: excluded from every publication pool.
    social_candidates = [a for a in candidates if a.panel == "social"]
    candidates = [a for a in candidates if a.panel != "social"]

    issues = []
    if selection_mode == "latest":
        source_ids = _parse_source_ids(latest_source_ids) or default_latest_source_ids(all_sources)
        fallback_source_ids = (
            default_latest_source_ids(all_sources) - source_ids if latest_fill else set()
        )
        selected = select_latest_articles(
            candidates,
            all_sources,
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
    elif selection_mode == "consensus":
        selected, report = select_consensus_articles(
            candidates, limit=limit, use_llm=use_llm, source_sets=source_sets
        )
    elif selection_mode == "sectioned":
        selected, report = select_sectioned_articles(
            candidates,
            limit=limit,
            use_llm=use_llm,
            social_articles=social_candidates,
            rubric=rubric,
        )
    elif selection_mode == "rank" or not issue_radar:
        selected = rank_articles(candidates, limit=limit)
        report = build_quality_report(selected, candidates)
    else:
        issues, selected = build_issue_radar(candidates, limit=4, use_llm=use_llm)
        report = build_quality_report(selected, candidates)
    t_select = time.monotonic()
    overview = ""
    if use_llm:
        # Sectioned mode standardizes every article body on the purpose-driven
        # skeleton (무슨 일 / 섹션 특화 / 다른 곳의 움직임 / 우리에게 미치는 영향).
        selected = enrich_with_openai(selected, structured=(selection_mode == "sectioned"))
        if selection_mode == "sectioned":
            overview = generate_weekly_overview(selected)
            flags = grounding_flags(selected)
            report["grounding_flags"] = flags
            if flags:
                console.print(
                    f"[yellow]근거 검증: 원문에 없는 숫자가 {len(flags)}개 섹션에서 발견 — "
                    f"generation_report.json의 grounding_flags를 검토하세요.[/yellow]"
                )
    report["issues"] = [issue.model_dump(mode="json") for issue in issues]
    if use_llm:
        report = evaluate_with_openai(selected, report)
    t_enrich = time.monotonic()

    report["overview"] = overview  # persisted so `rerender` can rebuild without LLM
    report["theme"] = theme
    report["thumbnails"] = thumbs
    report["rubric"] = rubric
    report["usage"] = usage.summary()
    report["timing_sec"] = {
        "collect": round(t_collect - t0, 1),
        "select": round(t_select - t_collect, 1),
        "enrich_llm": round(t_enrich - t_select, 1),
        "subtotal_pre_render": round(t_enrich - t0, 1),
    }

    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=days)
    output_dir = make_output_dir(output, period_end)
    package = write_package(
        output_dir, period_start, period_end, candidates, selected, report,
        issues=issues, overview=overview, capture=capture, theme=theme, thumbnails=thumbs,
    )
    t_render = time.monotonic()
    u = report["usage"]
    console.print(f"[bold green]Created[/bold green] {package.output_dir / 'newsletter.html'}")
    console.print(f"[bold green]Created[/bold green] {package.output_dir.with_suffix('.zip')}")
    console.print(
        f"[dim]tokens: {u['total_tokens']:,} (in {u['input_tokens']:,} / out {u['output_tokens']:,}, "
        f"{u['openai_calls']} calls)  |  time: {round(t_render - t0, 1)}s "
        f"(collect {round(t_collect - t0, 1)} / select {round(t_select - t_collect, 1)} / "
        f"enrich {round(t_enrich - t_select, 1)} / render {round(t_render - t_enrich, 1)})[/dim]"
    )


def _parse_source_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _pick_output_dir() -> Path | None:
    """최근 산출물 폴더를 번호로 고르게 합니다 (rerender/capture 용)."""
    from rich.prompt import Prompt

    candidates: list[Path] = []
    for base in sorted(Path(".").glob("out*")):
        if not base.is_dir():
            continue
        for d in base.glob("*_weekly_ai_newsletter*"):
            if d.is_dir() and (d / "data" / "selected_articles.json").exists():
                candidates.append(d)
    candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    candidates = candidates[:8]
    if not candidates:
        console.print("[yellow]재사용할 산출물이 없습니다. 먼저 build를 실행하세요.[/yellow]")
        return None
    console.print("대상 산출물을 고르세요 (최신순):")
    for i, d in enumerate(candidates, 1):
        console.print(f"  [bold]{i}[/bold]. {d}")
    console.print("  [bold]0[/bold]. 직접 경로 입력")
    pick = Prompt.ask(
        "번호",
        choices=[str(i) for i in range(0, len(candidates) + 1)],
        default="1",
        show_choices=False,
    )
    if pick == "0":
        return Path(Prompt.ask("산출물 폴더 경로"))
    return candidates[int(pick) - 1]


_THEME_MENU = "1. editorial(시안A)  2. magazine(시안B)  3. report(시안C)  4. classic(기존)"
_THEME_BY_PICK = {"1": "editorial", "2": "magazine", "3": "report", "4": "classic"}


def _interactive() -> None:
    """대화형으로 옵션을 골라 주간 뉴스레터를 생성합니다 (플래그를 외울 필요 없음)."""
    import os

    from rich.prompt import Confirm, IntPrompt, Prompt

    console.print("[bold cyan]AI 뉴스레터 — 대화형 모드[/bold cyan]")
    console.print("[dim]Enter를 누르면 대괄호 안의 기본값이 사용됩니다.[/dim]\n")

    console.print("작업을 고르세요:")
    console.print("  [bold]1[/bold]. 새 뉴스레터 생성 — 수집 + LLM 편집 + 렌더링 (build)")
    console.print("  [bold]2[/bold]. 재렌더 — 기존 데이터로 HTML만 다시, 테마·썸네일 변경 가능 (rerender)")
    console.print("  [bold]3[/bold]. 캡처 — 기존 HTML로 PNG·게시 패키지 생성 (capture)")
    console.print("  [bold]4[/bold]. 벤치마크 — 선정 결과를 외신 1주 보도와 비교, 루브릭 개선 힌트 (benchmark)")
    action = Prompt.ask("번호", choices=["1", "2", "3", "4"], default="1", show_choices=False)

    if action == "2":
        target = _pick_output_dir()
        if target is None:
            raise typer.Exit()
        console.print(f"테마: 0. 원래 테마 유지  {_THEME_MENU}")
        pick = Prompt.ask("번호", choices=["0", "1", "2", "3", "4"], default="0", show_choices=False)
        theme_choice = "" if pick == "0" else _THEME_BY_PICK[pick]
        console.print("썸네일: 0. 원래 설정 유지  1. 켬  2. 끔")
        thumb_pick = Prompt.ask("번호", choices=["0", "1", "2"], default="0", show_choices=False)
        thumb_choice = {"0": None, "1": True, "2": False}[thumb_pick]
        with_capture = Confirm.ask("PNG 캡처·게시 패키지까지 생성할까요?", default=False)
        rerender(
            output_dir=target,
            capture=with_capture,
            theme=theme_choice,
            assign_sections=False,
            thumbs=thumb_choice,
        )
        return

    if action == "3":
        target = _pick_output_dir()
        if target is None:
            raise typer.Exit()
        capture(output_dir=target)
        return

    if action == "4":
        target = _pick_output_dir()
        if target is None:
            raise typer.Exit()
        benchmark(
            output_dir=target,
            reference_sources=Path("config/sources.reference.yaml"),
            days=7,
        )
        return

    console.print("\n선별 모드를 고르세요:")
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

    include_candidates = Confirm.ask(
        "후보 소스(config/sources.candidate.yaml)도 포함할까요?", default=False
    )
    include_social = Confirm.ask(
        "소셜 신호 소스(YouTube/HN/Reddit — 게시 제외, 부스팅 전용)도 포함할까요?",
        default=(selection_mode == "sectioned"),
    )

    console.print(f"디자인 테마: {_THEME_MENU}")
    theme_pick = Prompt.ask("번호", choices=["1", "2", "3", "4"], default="1", show_choices=False)
    theme = _THEME_BY_PICK[theme_pick]

    do_capture = Confirm.ask("PNG 캡처·게시 패키지까지 생성할까요?", default=True)
    do_thumbs = Confirm.ask("본지 기사 행에 대표 이미지 썸네일을 넣을까요?", default=True)

    console.print("채점 루브릭: 1. standard(기존)  2. sota(SOTA 라이프사이클 가중 — 비교 실험용)")
    rubric_pick = Prompt.ask("번호", choices=["1", "2"], default="1", show_choices=False)
    rubric = {"1": "standard", "2": "sota"}[rubric_pick]

    output = Prompt.ask("출력 폴더", default="outputs")

    console.print("\n[bold]설정 요약[/bold]")
    console.print(
        f"  모드=[cyan]{selection_mode}[/cyan]  기간={days}일  기사수={limit}  "
        f"LLM={'예' if use_llm else '아니오'}  테마=[cyan]{theme}[/cyan]  "
        f"소셜신호={'예' if include_social else '아니오'}  후보소스={'예' if include_candidates else '아니오'}  "
        f"PNG캡처={'예' if do_capture else '아니오'}  출력={output}"
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
        include_candidates=include_candidates,
        include_social=include_social,
        capture=do_capture,
        theme=theme,
        thumbs=do_thumbs,
        rubric=rubric,
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _load_output_data(output_dir: Path):
    """기존 산출물 폴더의 data/*.json을 다시 모델로 로드합니다 (rerender/capture 공용)."""
    import json as _json
    import re as _re

    from .models import Article, Issue, RankedArticle

    data_dir = output_dir / "data"
    if not (data_dir / "selected_articles.json").exists():
        console.print(f"[red]data/selected_articles.json이 없습니다: {output_dir}[/red]")
        raise typer.Exit(1)
    selected = [
        RankedArticle(**row)
        for row in _json.loads((data_dir / "selected_articles.json").read_text(encoding="utf-8"))
    ]
    candidates_path = data_dir / "crawled_articles.json"
    candidates = (
        [Article(**row) for row in _json.loads(candidates_path.read_text(encoding="utf-8"))]
        if candidates_path.exists()
        else list(selected)
    )
    report: dict[str, object] = {}
    report_path = data_dir / "generation_report.json"
    if report_path.exists():
        report = _json.loads(report_path.read_text(encoding="utf-8"))
    issues = [Issue(**row) for row in report.get("issues", []) if isinstance(row, dict)]

    # Recover the period from the folder name (YYYY-MM-DD_weekly_ai_newsletter).
    match = _re.match(r"(\d{4})-(\d{2})-(\d{2})", output_dir.name)
    if match:
        period_end = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc
        )
    else:
        period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=7)
    return candidates, selected, report, issues, period_start, period_end


@app.command()
def rerender(
    output_dir: Path = typer.Argument(
        ..., help="기존 산출물 폴더 (예: outputs/2026-07-06_weekly_ai_newsletter)"
    ),
    capture: bool = typer.Option(
        False, help="PNG 캡처·게시 패키지까지 생성 (기본: HTML만 빠르게 재생성)."
    ),
    theme: str = typer.Option(
        "", help="디자인 테마 (classic|editorial|magazine|report). 비우면 원래 빌드의 테마 유지."
    ),
    assign_sections: bool = typer.Option(
        False,
        "--assign-sections",
        help="구(sectioned 이전) 데이터에 섹션을 휴리스틱으로 배정 — 새 섹션 레이아웃을 기존 데이터로 미리보기할 때 사용.",
    ),
    thumbs: bool | None = typer.Option(
        None, "--thumbs/--no-thumbs", help="썸네일 표시 여부. 지정 안 하면 원래 빌드 설정 유지."
    ),
) -> None:
    """수집·LLM 없이 기존 산출물의 data/*.json으로 HTML을 다시 생성합니다.

    디자인 템플릿 반복 작업용 — 기본은 HTML만 재생성하며, PNG까지 필요하면
    --capture를 붙이거나 별도의 `capture` 명령을 사용합니다.
    """
    _enable_os_trust_store()
    candidates, selected, report, issues, period_start, period_end = _load_output_data(output_dir)
    if assign_sections:
        from .sections import SECTION_ORDER, assign_section

        assigned = 0
        for article in selected:
            if article.section not in SECTION_ORDER:
                article.section = assign_section(article)
                assigned += 1
        if assigned:
            console.print(f"[cyan]섹션 휴리스틱 배정: {assigned}건 (미리보기용 — LLM 배정 아님)[/cyan]")
    package = write_package(
        output_dir,
        period_start,
        period_end,
        candidates,
        selected,
        report,
        issues=issues,
        overview=str(report.get("overview") or ""),
        capture=capture,
        theme=theme or str(report.get("theme") or "editorial"),
        thumbnails=bool(report.get("thumbnails", True)) if thumbs is None else thumbs,
    )
    console.print(f"[bold green]Re-rendered[/bold green] {package.output_dir / 'newsletter.html'}")
    if not capture:
        console.print("[dim]PNG는 생성하지 않았습니다. 필요 시: ai-newsletter capture <폴더>[/dim]")


@app.command()
def capture(
    output_dir: Path = typer.Argument(
        ..., help="기존 산출물 폴더 (예: outputs/2026-07-06_weekly_ai_newsletter)"
    ),
) -> None:
    """이미 렌더링된 HTML로 PNG 캡처와 게시 패키지(publish_ready)만 생성합니다.

    HTML은 다시 만들지 않습니다. playwright chromium이 필요합니다
    (`uv run playwright install chromium` 1회).
    """
    from .models import NewsletterPackage
    from .render import capture_package

    _enable_os_trust_store()
    if not (output_dir / "newsletter.html").exists():
        console.print(f"[red]newsletter.html이 없습니다. 먼저 build 또는 rerender를 실행하세요: {output_dir}[/red]")
        raise typer.Exit(1)
    _, selected, report, issues, period_start, period_end = _load_output_data(output_dir)
    package = NewsletterPackage(
        period_start=period_start,
        period_end=period_end,
        title=f"AI 주간 뉴스레터 | {period_start:%Y.%m.%d} - {period_end:%Y.%m.%d}",
        overview=str(report.get("overview") or ""),
        thumbnails=bool(report.get("thumbnails", True)),
        articles=selected,
        issues=issues,
        quality_report=report,
        output_dir=output_dir,
    )
    capture_package(package)
    manifest_path = output_dir / "board" / "image_post" / "image_manifest.json"
    if manifest_path.exists():
        import json as _json

        rows = _json.loads(manifest_path.read_text(encoding="utf-8"))
        created = sum(1 for r in rows if r.get("created"))
        color = "green" if created == len(rows) else "yellow"
        console.print(f"[{color}]PNG 캡처: {created}/{len(rows)} 성공[/{color}]")
        if created < len(rows):
            first_fail = next((r["message"] for r in rows if not r.get("created")), "")
            console.print(f"[yellow]실패 사유: {first_fail}[/yellow]")
    console.print(f"[bold green]Captured[/bold green] {output_dir.with_suffix('.zip')}")


@app.command()
def benchmark(
    output_dir: Path = typer.Argument(
        ..., help="비교할 산출물 폴더 (예: outputs/2026-07-06_weekly_ai_newsletter_v4)"
    ),
    reference_sources: Path = typer.Option(
        Path("config/sources.reference.yaml"), help="레퍼런스 매체 패널 YAML."
    ),
    days: int = typer.Option(7, min=1, max=31, help="레퍼런스 수집 기간(일)."),
) -> None:
    """선정 결과를 공신력 있는 외신 1주 보도와 비교합니다 (루브릭 재귀 개선 루프).

    결과: data/benchmark_report.json + benchmarks/history.jsonl 누적.
    missed_hot_topics(외신 다수가 다뤘는데 우리가 놓친 토픽)가 루브릭 수정의 재료입니다.
    """
    import json as _json

    from .benchmark import compare_with_reference

    _enable_os_trust_store()
    selected_path = output_dir / "data" / "selected_articles.json"
    if not selected_path.exists():
        console.print(f"[red]data/selected_articles.json이 없습니다: {output_dir}[/red]")
        raise typer.Exit(1)
    selected_rows = _json.loads(selected_path.read_text(encoding="utf-8"))

    source_list = load_sources(reference_sources)
    collector = Collector(options=CollectionOptions(per_source_limit=40))
    reference = []
    try:
        for source in source_list.sources:
            if not source.enabled:
                continue
            try:
                rows = collector.collect(source, days)
                reference.extend(rows)
                console.print(f"[green]OK[/green] {source.name}: {len(rows)}")
            except Exception as exc:
                console.print(f"[yellow]WARN[/yellow] {source.name}: {exc}")
    finally:
        collector.close()

    result = compare_with_reference(selected_rows, reference)
    report_meta = {}
    report_path = output_dir / "data" / "generation_report.json"
    if report_path.exists():
        gen = _json.loads(report_path.read_text(encoding="utf-8"))
        report_meta = {"mode": gen.get("mode"), "rubric": gen.get("rubric")}
    result["build"] = {"output_dir": str(output_dir), **report_meta}

    (output_dir / "data" / "benchmark_report.json").write_text(
        _json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    history_dir = Path("benchmarks")
    history_dir.mkdir(exist_ok=True)
    with (history_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            _json.dumps(
                {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "output_dir": str(output_dir),
                    "alignment_rate": result["alignment_rate"],
                    "missed_count": len(result["missed_hot_topics"]),
                    **report_meta,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    console.print(
        f"\n[bold]일치도: {result['alignment_rate']:.0%}[/bold] "
        f"(선정 {result['selected_count']}건 중 {len(result['aligned'])}건을 외신도 보도)"
    )
    if result["missed_hot_topics"]:
        console.print("[yellow]외신 다수가 다뤘는데 우리가 놓친 토픽 (루브릭 개선 힌트):[/yellow]")
        for miss in result["missed_hot_topics"][:5]:
            console.print(f"  - {miss['topic_key']} ({len(miss['outlets'])}개 매체) — {miss['sample_title']}")
    if result["ours_only"]:
        console.print(f"[dim]우리만 뽑은 기사 {len(result['ours_only'])}건 — 차별점인지 과대평가인지 검토[/dim]")
    console.print(f"[bold green]Saved[/bold green] {output_dir / 'data' / 'benchmark_report.json'}")


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
