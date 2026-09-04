#!/bin/zsh
# 화요일 주간 자동화의 기계 파이프라인 (Mac).
#
# 사용법:
#   scripts/run_weekly_mac.sh build              빌드 + 산출물 검증 (산출 폴더를 stdout 마지막 줄에 출력)
#   scripts/run_weekly_mac.sh publish <산출폴더>  사이트 배포 → push → 델타 zip → 릴리스 업로드
#   scripts/run_weekly_mac.sh notify "<메시지>"   macOS 알림
#
# 판단이 필요한 일(근거 플래그 원문 대조, 문체 윤문)은 이 스크립트가 하지 않는다 —
# .claude/commands/weekly-run.md 런북이 build와 publish 사이에서 Claude로 수행한다.
#
# 트랙: official(기본)은 --history를 적용한 빌드를 정식 주차 폴더로 배포하고
# 정식 릴리스(--latest, 고정명 newsletter_delta.zip 포함)를 올린다.
# 2026-09-04 사용자 결정: 이력 적용 버전이 곧 정식 버전 — 비교판 운영 종료.
# TRACK=history는 <주차>-history 비교판 배포용으로 남겨 둔 실험 모드다.

set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="$HOME/workspace/ai-newsletter-automation"
SITE="$HOME/workspace/ai-newsletter-site"
TRACK="${TRACK:-official}"                # official(정식, 기본) | history(실험용 비교판)
OFFICIAL_USES_HISTORY="${OFFICIAL_USES_HISTORY:-1}"  # 정식 빌드에 --history 적용
MAX_ZIP_MB=20

WEEK="$(date +%Y-%m-%d)"
cd "$REPO"

notify() {
  osascript -e "display notification \"$1\" with title \"AI 뉴스레터 자동화\"" || true
}

fail() {
  echo "[실패] $1" >&2
  notify "실패: $1"
  exit 1
}

case "${1:-}" in

build)
  build_args=(--days 7 --limit 10)
  if [[ "$TRACK" == "history" || "$OFFICIAL_USES_HISTORY" == "1" ]]; then
    build_args+=(--history)
  fi
  echo "[빌드] uv run ai-newsletter build ${build_args[*]} (트랙: $TRACK)"
  uv run ai-newsletter build "${build_args[@]}" 2>&1 | tee "/tmp/ai-newsletter-build-$WEEK.log"

  # 오늘 날짜의 가장 최신 버전 폴더(_vN 포함)를 찾는다.
  out_dir="$(find "$REPO/outputs" -maxdepth 1 -type d -name "${WEEK}_weekly_ai_newsletter*" | sort -V | tail -1)"
  [[ -n "$out_dir" ]] || fail "오늘 날짜 산출물 폴더가 없습니다"

  # 산출물 검증 — 판단이 아닌 기계적 확인만.
  uv run python - "$out_dir" <<'PY' || fail "산출물 검증 실패 (위 로그 참조)"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
rows = json.loads((out / "data" / "selected_articles.json").read_text())
report = json.loads((out / "data" / "generation_report.json").read_text())
errors = []
if len(rows) != 10:
    errors.append(f"선정 기사 수 {len(rows)} != 10")
if report.get("candidate_count", 0) < 60:
    errors.append(f"후보 수 부족: {report.get('candidate_count')}")
for i, row in enumerate(rows, 1):
    img = row.get("local_image")
    if img and not (out / img).exists():
        errors.append(f"기사 {i} 대표 이미지 파일 없음: {img}")
    if img and (out / img).exists() and (out / img).stat().st_size < 10_000:
        errors.append(f"기사 {i} 대표 이미지가 비정상적으로 작음: {img}")
if not (out / "newsletter.html").exists():
    errors.append("newsletter.html 없음")
if errors:
    print("\n".join("  - " + e for e in errors))
    sys.exit(1)
flags = report.get("grounding_flags") or []
style = report.get("style_flags") or {}
print(f"검증 통과: 기사 10건, 후보 {report.get('candidate_count')}건")
print(f"판단 필요 항목 — 근거 플래그 {len(flags)}건, 문체 강 {style.get('strong_count', 0)} / 중 {style.get('weak_count', 0)}건")
PY
  echo "$out_dir"
  ;;

publish)
  out_dir="${2:-}"
  [[ -d "$out_dir" ]] || fail "publish: 산출물 폴더를 인자로 주세요"

  if [[ "$TRACK" == "history" ]]; then
    folder="$WEEK-history"
    tag="weekly-$WEEK-history"
    latest_args=(--latest=false)
  else
    folder="$WEEK"
    tag="weekly-$WEEK"
    latest_args=(--latest)
  fi

  echo "[사이트] $folder 로 배포"
  site_log="$(uv run python scripts/build_site.py "$out_dir" "$SITE" "$folder")"
  echo "$site_log"
  echo "$site_log" | grep -q "외부 리소스 참조: 0건" || fail "외부 리소스 참조가 남아 있음"

  echo "[git] 커밋·push"
  git -C "$SITE" add -A
  if git -C "$SITE" diff --cached --quiet; then
    echo "  변경 없음 — 커밋 생략"
  else
    git -C "$SITE" commit -q -m "site: $folder weekly auto ($TRACK track)"
  fi
  git -C "$SITE" push -q origin main

  echo "[zip] 델타 zip 생성"
  uv run python scripts/make_delta_zip.py "$SITE" "$folder"
  zip_path="$HOME/workspace/newsletter_delta_$folder.zip"
  [[ -f "$zip_path" ]] || fail "델타 zip이 생성되지 않음"

  # zip 검증: 필수 항목 포함, 잡파일 없음, 크기 상한.
  toc="$(unzip -l "$zip_path")"
  echo "$toc" | grep -q "$folder/index.html" || fail "zip에 주차 index.html 누락"
  echo "$toc" | grep -q "post_meta.json" || fail "zip에 post_meta.json 누락"
  ! echo "$toc" | grep -q ".DS_Store" || fail "zip에 .DS_Store 포함"
  size_mb=$(( $(stat -f%z "$zip_path") / 1000000 ))
  [[ "$size_mb" -le "$MAX_ZIP_MB" ]] || fail "zip이 ${MAX_ZIP_MB}MB 초과: ${size_mb}MB"

  echo "[릴리스] $tag"
  if gh release view "$tag" --repo ilovejsbach/ai-newsletter-site >/dev/null 2>&1; then
    gh release delete "$tag" --repo ilovejsbach/ai-newsletter-site --yes
  fi
  release_files=("$zip_path")
  if [[ "$TRACK" == "official" ]]; then
    cp "$zip_path" /tmp/newsletter_delta.zip   # 즐겨찾기 고정 주소용 고정명 자산
    release_files+=(/tmp/newsletter_delta.zip)
  fi
  ( cd "$SITE" && gh release create "$tag" "${release_files[@]}" \
      --title "$folder 주간 델타" \
      --notes "내부망 반입용 델타 zip (트랙: $TRACK)" "${latest_args[@]}" )

  url="https://github.com/ilovejsbach/ai-newsletter-site/releases/download/$tag/newsletter_delta_$folder.zip"
  curl -sfL "$url" -o /tmp/delta_dl_check.zip || fail "릴리스 다운로드 확인 실패: $url"
  unzip -tq /tmp/delta_dl_check.zip >/dev/null || fail "다운로드한 zip 손상"
  echo "[완료] 다운로드 확인: $url"
  notify "주간 배포 완료 ($folder, $TRACK 트랙)"
  ;;

notify)
  notify "${2:-알림}"
  ;;

*)
  sed -n '2,14p' "$0"
  exit 1
  ;;
esac
