"""주간 델타 zip 생성 — 내부망으로 옮길 '변경분만' 묶는다.

사용법: uv run python scripts/make_delta_zip.py <사이트_저장소> [주차(YYYY-MM-DD)]
        (주차 생략 시 가장 최신 주차 폴더)

내부망 절차가 "전체 zip에서 3개 골라 복사"가 아니라 "델타 zip을 git 작업
폴더에 그대로 풀고 push"가 되도록, zip 안 경로를 저장소 루트 기준으로 담는다.

포함 규칙:
- <주차>/ 폴더 전체 (신규)
- index.html, latest/index.html (매주 갱신)
- 보너스: 직전 주차 반영 이후 git에서 바뀐 그 외 파일(예: .gitlab-ci.yml 수정,
  MANUAL 갱신)을 자동 감지해 함께 담고 목록을 출력한다 — 수동 선별 누락 방지.
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _git(site: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(site), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    site = Path(sys.argv[1]).resolve()
    weeks = sorted(d.name for d in site.iterdir() if d.is_dir() and WEEK_RE.match(d.name))
    if not weeks:
        sys.exit("주차 폴더(YYYY-MM-DD)가 없습니다.")
    week = sys.argv[2] if len(sys.argv) > 2 else weeks[-1]
    if week not in weeks:
        sys.exit(f"주차 폴더가 없습니다: {week}")

    include: list[Path] = []
    for name in (week, "index.html", "latest/index.html"):
        path = site / name
        if path.is_dir():
            include.extend(p for p in sorted(path.rglob("*")) if p.is_file() and p.name != ".DS_Store")
        elif path.is_file():
            include.append(path)

    # 직전 주차 반영 이후 바뀐 그 외 파일 자동 포함 (git 이력 기반).
    extras: list[str] = []
    try:
        first_week_commit = _git(site, "log", "--format=%H", "--reverse", "--", week).splitlines()[0]
        baseline = _git(site, "rev-parse", f"{first_week_commit}^")
        changed = _git(site, "diff", "--name-only", baseline, "HEAD").splitlines()
        already = {str(p.relative_to(site)) for p in include}
        for name in changed:
            path = site / name
            if name not in already and path.is_file() and not name.startswith(week):
                include.append(path)
                extras.append(name)
    except (subprocess.CalledProcessError, IndexError):
        print("[안내] git 이력 조회 실패 — 기본 3종만 담습니다.")

    out = site.parent / f"newsletter_delta_{week}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in include:
            zf.write(path, path.relative_to(site))

    print(f"델타 zip: {out} ({out.stat().st_size / 1e6:.1f}MB, {len(include)}개 파일)")
    if extras:
        print("자동 포함된 추가 변경 파일:", ", ".join(extras))
    print("내부망 절차: git 작업 폴더에서 git pull → 이 zip을 폴더에 그대로 풀기 → git add -A → commit → push")


if __name__ == "__main__":
    main()
