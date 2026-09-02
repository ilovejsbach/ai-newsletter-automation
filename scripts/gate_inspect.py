"""판단 게이트 보조: 산출물 폴더의 이미지 체크섬 기록/비교와 선정 기사 원문 덤프.

사용법:
  uv run python scripts/gate_inspect.py <산출폴더> snapshot        이미지 md5를 /tmp/gate_img_md5.json에 기록
  uv run python scripts/gate_inspect.py <산출폴더> diff            기록과 현재 이미지 md5 비교
  uv run python scripts/gate_inspect.py <산출폴더> article <N>     N번째 선정 기사의 원문·요약·생성 본문 출력
"""
import hashlib
import json
import sys
from pathlib import Path

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
SNAP = Path("/tmp/gate_img_md5.json")


def image_md5s(out: Path) -> dict[str, str]:
    rows = {}
    for sub in ("assets", "articles", "board"):
        d = out / sub
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in IMG_EXT:
                rows[str(p.relative_to(out))] = hashlib.md5(p.read_bytes()).hexdigest()
    return rows


def main() -> None:
    out = Path(sys.argv[1])
    mode = sys.argv[2]
    if mode == "snapshot":
        rows = image_md5s(out)
        SNAP.write_text(json.dumps(rows, indent=1))
        print(f"이미지 {len(rows)}개 체크섬 기록 -> {SNAP}")
    elif mode == "diff":
        before = json.loads(SNAP.read_text())
        after = image_md5s(out)
        changed = [k for k in before if after.get(k) != before[k]]
        added = [k for k in after if k not in before]
        print(f"변경 {len(changed)} / 추가 {len(added)} / 전체 {len(after)}")
        for k in changed:
            print("  변경:", k)
        for k in added:
            print("  추가:", k)
        sys.exit(1 if changed else 0)
    elif mode == "article":
        idx = int(sys.argv[3]) - 1
        rows = json.loads((out / "data" / "selected_articles.json").read_text())
        row = rows[idx]
        print("== keys:", list(row.keys()))
        for k, v in row.items():
            if k in ("body", "summary", "content", "text", "full_text"):
                continue
            if isinstance(v, str):
                print(f"\n== [{k}]\n{v}")
            elif isinstance(v, (list, dict)):
                s = json.dumps(v, ensure_ascii=False, indent=1)
                print(f"\n== [{k}]\n{s[:8000]}")
        for k in ("summary", "body", "content", "text", "full_text"):
            if row.get(k):
                print(f"\n== [{k}] (len {len(row[k])})\n{row[k][:15000]}")
    elif mode == "followups":
        rows = json.loads((out / "data" / "selected_articles.json").read_text())
        for i, row in enumerate(rows, 1):
            print(f"{i:2d}. [{row.get('section')}] {row.get('korean_title')} | followup_of={row.get('followup_of') or '-'}")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
