"""`audit-kit arch ...` 명령 구현."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile

from audit_kit._proc import no_window_kwargs
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from audit_kit.arch.doc import importlinter_contracts, render_markdown
from audit_kit.arch.scan import RULE_LABEL, scan
from audit_kit.arch.spec import SPEC_FILE, infer_spec, load_spec, render_spec
from audit_kit.cliargs import add_fix_flags, add_path, add_undo
from audit_kit.config import load_config
from audit_kit.runner import run_module
from audit_kit.scope import build_project_graph, get_scope

DOC_FILE = "ARCHITECTURE.md"


def _need_spec(cfg):
    spec = load_spec(cfg.root)
    if spec is None:
        print(
            f"{SPEC_FILE} 이 없습니다. 먼저 `audit-kit arch init` 으로 설계 초안을 만드세요.",
            file=sys.stderr,
        )
    return spec


def _write_doc(cfg, spec, graph=None, violations=None):
    graph = graph or build_project_graph(cfg)
    violations = scan(cfg, spec, graph) if violations is None else violations
    (cfg.root / DOC_FILE).write_text(
        render_markdown(cfg.root.name, spec, graph, violations), encoding="utf-8"
    )
    return violations


def cmd_init(args) -> int:
    cfg = load_config(args.path)
    f = cfg.root / SPEC_FILE
    if f.exists() and not args.force:
        print(
            f"{SPEC_FILE} 이 이미 있습니다 (--force 로 새 초안). 현재 설계 확인: audit-kit arch check"
        )
        return 1
    if not cfg.packages:
        print("패키지를 찾지 못했습니다. [tool.audit-kit] packages 를 지정하세요.", file=sys.stderr)
        return 2
    graph = build_project_graph(cfg)
    spec, unassigned = infer_spec(graph, cfg.packages, get_scope(cfg))
    f.write_text(render_spec(spec, unassigned), encoding="utf-8")
    spec = load_spec(cfg.root)
    assert spec is not None, f"방금 쓴 {SPEC_FILE} 을 다시 읽지 못했습니다"
    violations = _write_doc(cfg, spec, graph)
    print(f"+ {SPEC_FILE} 설계 초안 생성")
    for i, lay in enumerate(spec.layers, 1):
        print(f"    {i}. {lay.name:<11} {', '.join(lay.modules)}")
    if spec.entrypoints:
        print(f"    진입점      {', '.join(spec.entrypoints)}")
    if unassigned:
        print(
            f"  ! 미배정 {len(unassigned)}개: {', '.join(unassigned)}  → 설계 파일에서 계층을 정해 주세요"
        )
    print(f"+ {DOC_FILE} 생성 (현재 위반 {len(violations)}건)")
    print("\n다음: 설계 파일 검토·수정 → audit-kit arch check → audit-kit arch fix")
    print("      (Claude Code 에서는 /arch 로 설계 검토부터 수정까지 진행)")
    return 0


def print_violations(violations: list):
    groups = defaultdict(list)
    for v in violations:
        groups[v.rule].append(v)
    for rule, vs in groups.items():
        print(f"\n[{RULE_LABEL.get(rule, rule)}] {len(vs)}건")
        for v in vs:
            loc = f"{v.file}:{v.line}" if v.line else (v.file or "")
            ctx = {"lazy": " (지연)", "type_checking": " (TYPE_CHECKING)"}.get(v.context, "")
            print(f"  {loc} — {v.message}{ctx}")
            if v.hint:
                print(f"      → {v.hint}")


def cmd_check(args) -> int:
    cfg = load_config(args.path)
    spec = _need_spec(cfg)
    if spec is None:
        return 2
    violations = scan(cfg, spec)
    if args.json:
        print(json.dumps([v.__dict__ for v in violations], ensure_ascii=False, indent=2))
    elif not violations:
        print("설계 위반 없음 ✅")
    else:
        print(f"설계 위반 {len(violations)}건")
        print_violations(violations)
    return 1 if violations else 0


def _git_dirty(root: Path, files: list) -> list:
    try:
        p = subprocess.run(
            ["git", "status", "--porcelain", "--", *files],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if p.returncode != 0:  # git 저장소 아님
        return []
    return [ln[3:] for ln in p.stdout.splitlines() if ln.strip()]


def _failing_tests(root: Path, timeout: int) -> set:
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "j.xml"
        run_module(
            "pytest", ["-q", "-p", "no:cacheprovider", f"--junitxml={junit}"], root, timeout=timeout
        )
        if not junit.exists():
            return {"<pytest 실행 실패>"}
        out = set()
        for tc in ET.parse(junit).getroot().iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                out.add(f"{tc.get('classname')}.{tc.get('name')}")
        return out


@dataclass
class FixOutput:
    """`arch fix`/`tangle --fix`/`struct fix` 가 실행 뒤 만든 결과물(리포트 폴더·바뀐 파일·diff).
    호출부에서 항상 세 값을 같이 만들어 넘기므로 하나로 묶었다(PLR0913, 2026-09-28)."""

    out_dir: Path
    changed: list
    diffs: str


def render_fix_report(title: str, res, diffs: str) -> str:
    """`struct fix`/`std fix` 가 공유하는 자동 수정 리포트 마크다운(제목만 다름).
    `res`는 `applied: list[(rule, desc)]`·`failed: list[(rule, what, why)]`를 갖는 결과 객체면 된다
    (구체적 타입은 호출부마다 다름 — StructFixResult/StdFixResult, 2026-09-28 Stop hook 중복
    코드 지적으로 합침)."""
    return (
        f"# {title}\n\n"
        + "\n".join(f"- {d}" for _, d in res.applied)
        + "\n\n## 실패\n\n"
        + ("\n".join(f"- {w}: {y}" for _, w, y in res.failed) or "없음")
        + "\n\n## 변경 내용\n\n```diff\n"
        + diffs
        + "\n```\n"
    )


@dataclass
class ApplyLabels:
    """서브커맨드별로 다른 안내 문구만 모음(위 셋과 같은 이유로 분리)."""

    preview_cmd: str
    marker_file: str = "ARCH_FIX_LATEST.txt"
    undo_cmd: str = "audit-kit arch undo"


def _apply_or_preview(cfg, res, out: FixOutput, args, labels: ApplyLabels) -> int:
    """미리보기 출력 또는(--apply) 커밋 상태 확인 → (옵션)테스트 비교 → 반영.
    `arch fix`/`tangle --fix`/`struct fix` 가 Workspace/FixResult 를 똑같은 방식으로 반영하므로
    공용화함(원래 세 곳에 거의 동일하게 복사돼 있던 걸 여기 하나로 합침 — struct fix 쪽은
    2026-09-28 Stop hook 중복 코드 지적으로 추가). `args`(--apply/--verify/--force/--quiet)를
    그대로 받는다 — 서브커맨드들이 옵션 이름을 똑같이 맞춰 뒀으므로(arch fix 와 동일한 사용자
    경험을 주려는 설계 의도) 낱개로 풀어 넘기면 호출부 자체가 또 복제된다."""
    from audit_kit.arch.fix import write_back

    changed = out.changed
    if not changed:
        return 0 if not res.remaining else 1
    if not args.apply:
        if not args.quiet:
            print("\n" + out.diffs)
        print(f"미리보기입니다. 적용: {labels.preview_cmd}")
        return 0
    dirty = _git_dirty(cfg.root, changed)
    if dirty and not args.force:
        print(
            f"\n커밋되지 않은 변경이 있는 파일: {', '.join(dirty)}\n"
            "먼저 커밋하거나 --force 로 진행하세요 (백업은 항상 만들어짐).",
            file=sys.stderr,
        )
        return 1
    if args.verify:
        print("\n테스트 비교 중 (원본 vs 수정본)...")
        before = _failing_tests(cfg.root, cfg.pytest_timeout)
        after = _failing_tests(res.workspace.tmp, cfg.pytest_timeout)
        new = sorted(after - before)
        if new:
            print(
                "수정 후 새로 실패하는 테스트가 있어 적용하지 않습니다:\n  " + "\n  ".join(new),
                file=sys.stderr,
            )
            return 1
        print(f"  테스트 통과 상태 동일 (기존 실패 {len(before)}건)")
    write_back(res, out.out_dir / "backup")
    (cfg.root / cfg.report_dir / labels.marker_file).write_text(out.out_dir.name, encoding="utf-8")
    print(f"\n적용 완료: {len(changed)}개 파일. 되돌리기: {labels.undo_cmd}")
    return 0


def cmd_fix(args) -> int:
    from audit_kit.arch.fix import run_fix

    cfg = load_config(args.path)
    spec = _need_spec(cfg)
    if spec is None:
        return 2
    only = set(args.only.split(",")) if args.only else None
    print("작업공간(임시 복사본)에서 수정·검증 중...")
    res = run_fix(
        cfg, spec, allow_lazy=args.allow_lazy, import_check=not args.no_import_check, only=only
    )
    ws = res.workspace
    try:
        changed = ws.changed()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cfg.root / cfg.report_dir / f"arch-fix_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"\n위반 {res.before}건 → {len(res.remaining)}건 (자동 수정 {len(res.applied)}건, 파일 {len(changed)}개)"
        )
        for a in res.applied:
            print(f"  ✔ [{a.kind}] {a.description}")
        if res.unresolved:
            print("\n자동 수정 불가 (수동/Claude 필요):")
            for (rule, src, tgt, names), reasons in res.unresolved.items():
                print(f"  ✘ {RULE_LABEL.get(rule, rule)} {src} -> {tgt} {list(names) or ''}")
                for r in reasons:
                    print(f"      - {r}")
        other = [
            v
            for v in res.remaining
            if v.rule
            not in {
                "ARCH-LAYER",
                "ARCH-CYCLE",
                "ARCH-FORBIDDEN",
                "ARCH-INDEPENDENT",
                "ARCH-SIBLING",
                "ARCH-SKIP",
            }
        ]
        if other:
            print("\n설계 판단 필요:")
            print_violations(other)

        diffs = "".join(ws.diff(rel) for rel in changed)
        _write_report(out_dir, res, changed, diffs)
        print(f"\n리포트: {out_dir / 'arch-fix.md'}")

        rc = _apply_or_preview(
            cfg,
            res,
            FixOutput(out_dir, changed, diffs),
            args,
            ApplyLabels(preview_cmd="audit-kit arch fix --apply"),
        )
        if rc == 0 and args.apply and changed and (cfg.root / DOC_FILE).exists():
            _write_doc(cfg, spec)
        return rc
    finally:
        ws.cleanup()


def _write_report(out_dir: Path, res, changed: list, diffs: str):
    L = [
        "# 설계 위반 자동 수정 결과",
        "",
        f"- 위반: {res.before}건 → {len(res.remaining)}건",
        f"- 자동 수정: {len(res.applied)}건 / 변경 파일: {len(changed)}개",
        "",
        "## 적용한 수정",
        "",
    ]
    L += [f"{i}. [{a.kind}] {a.description}" for i, a in enumerate(res.applied, 1)] or ["없음"]
    L += ["", "## 남은 위반 (수동 수정 — Claude 는 /arch 스킬 절차로 한 건씩 처리)", ""]
    from audit_kit.arch.fix import k2

    for i, v in enumerate(res.remaining, 1):
        L.append(f"{i}. [{RULE_LABEL.get(v.rule, v.rule)}] {v.file}:{v.line} — {v.message}")
        if v.hint:
            L.append(f"   수정 방향: {v.hint}")
        for r in res.unresolved.get(k2(v), []):
            L.append(f"   자동 수정 불가: {r}")
    if not res.remaining:
        L.append("없음 ✅")
    L += ["", "## 변경 내용", "", "```diff", diffs.rstrip(), "```", ""]
    (out_dir / "arch-fix.md").write_text("\n".join(L), encoding="utf-8")


def cmd_undo(args) -> int:
    from audit_kit.arch.fix import undo

    cfg = load_config(args.path)
    latest = cfg.root / cfg.report_dir / "ARCH_FIX_LATEST.txt"
    if not latest.exists():
        print("되돌릴 arch fix 기록이 없습니다.", file=sys.stderr)
        return 1
    backup = cfg.root / cfg.report_dir / latest.read_text(encoding="utf-8").strip() / "backup"
    files = undo(cfg.root, backup)
    latest.unlink()
    print(f"복원: {len(files)}개 파일 ({', '.join(files)})")
    return 0


def cmd_doc(args) -> int:
    cfg = load_config(args.path)
    spec = _need_spec(cfg)
    if spec is None:
        return 2
    v = _write_doc(cfg, spec)
    print(f"{DOC_FILE} 갱신 (현재 위반 {len(v)}건)")
    return 0


def cmd_export(args) -> int:
    cfg = load_config(args.path)
    spec = _need_spec(cfg)
    if spec is None:
        return 2
    print(importlinter_contracts(spec))
    return 0


def register(sub):
    p = sub.add_parser("arch", help="프로그램 구조 설계 → 스캔 → 수정")
    asub = p.add_subparsers(dest="arch_cmd", required=True)

    q = asub.add_parser("init", help="현재 코드에서 architecture.toml 설계 초안 생성")
    add_path(q)
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_init)

    q = asub.add_parser("check", help="설계 대비 위반 검사")
    add_path(q)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_check)

    q = asub.add_parser("fix", help="위반 자동 수정 (기본 미리보기)")
    add_path(q)
    add_fix_flags(
        q,
        apply_help="원본에 적용 (백업 생성)",
        verify_help="적용 전 pytest 결과 비교, 새 실패 시 중단",
        force_help="커밋 안 된 파일이어도 적용",
    )
    q.add_argument(
        "--allow-lazy",
        action="store_true",
        help="순환을 함수 안 지연 임포트로 끊는 것 허용(임시 처방)",
    )
    q.add_argument("--only", help="전략 제한: reexport,type_only,unused,move")
    q.add_argument(
        "--no-import-check",
        action="store_true",
        help="변경 모듈 임포트 확인 생략(임포트 시 부작용 있는 프로젝트)",
    )
    q.set_defaults(func=cmd_fix)

    add_undo(asub, target="arch", func=cmd_undo)

    q = asub.add_parser("doc", help="ARCHITECTURE.md 갱신")
    add_path(q)
    q.set_defaults(func=cmd_doc)

    q = asub.add_parser("export-importlinter", help="설계를 import-linter 계약으로 출력")
    add_path(q)
    q.set_defaults(func=cmd_export)
