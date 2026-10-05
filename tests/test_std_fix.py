"""EFF-02(리스트 멤버십) 자동 수정(`audit-kit std --fix`) 시험.

합성 픽스처로 실제 파일을 만들어 `run_std_fix`를 실제로 돌려 확인한다(mock 아님).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from audit_kit.std import fix as std_fix

sys.path.insert(0, str(Path(__file__).parent))
from test_audit_kit import cfg_for, write

run_std_fix = std_fix.run_std_fix


def _mk(root: Path, body: str) -> None:
    write(root, {"pkg/__init__.py": "", "pkg/mod.py": textwrap.dedent(body)})


def test_fixes_local_assign_and_param_candidates(tmp_path):
    _mk(
        tmp_path,
        """
        def f(xs, allowed: list):
            out = []
            for x in xs:
                if x in allowed:
                    out.append(x)
            return out


        def g(xs):
            bad = ['a', 'b']
            return [x for x in xs if x in bad]
        """,
    )
    res = run_std_fix(cfg_for(tmp_path))
    try:
        assert res.before == 2
        assert res.after == 0
        assert len(res.applied) == 2
        assert not res.failed
        new_text = res.workspace.read("pkg/mod.py")
        assert "allowed_set = frozenset(allowed)" in new_text
        assert "bad_set = frozenset(bad)" in new_text
        assert "x in allowed_set" in new_text
        assert "x in bad_set" in new_text
        # 원래 이름은 그대로 남아있어야(다른 용도로 계속 쓰일 수 있으므로 원본을 지우지 않음)
        assert "allowed: list" in new_text
        compile(new_text, "pkg/mod.py", "exec")
    finally:
        res.workspace.cleanup()


def test_skips_when_rebound_in_scope(tmp_path):
    _mk(
        tmp_path,
        """
        def f(xs):
            seen = []
            for x in xs:
                if x in seen:
                    pass
            seen = compute_replacement()
            return seen
        """,
    )
    res = run_std_fix(cfg_for(tmp_path))
    try:
        assert res.before == 1
        assert res.after == 1  # 고치지 않았으니 그대로
        assert not res.applied
        assert len(res.failed) == 1
        assert "다시 대입" in res.failed[0][2]
    finally:
        res.workspace.cleanup()


def test_skips_when_passed_to_another_call_but_fixes_when_only_locally_used(tmp_path):
    _mk(
        tmp_path,
        """
        def helper(items):
            items.append(999)


        def unsafe(xs, risky: list):
            for x in xs:
                if x in risky:
                    pass
            helper(risky)


        def safe(xs, ok: list):
            for x in xs:
                if x in ok:
                    pass
            return len(ok)
        """,
    )
    res = run_std_fix(cfg_for(tmp_path))
    try:
        assert res.before == 2
        assert res.after == 1  # unsafe 는 그대로, safe 만 고침
        assert len(res.applied) == 1
        assert "'ok'" in res.applied[0][1]
        assert len(res.failed) == 1
        assert "risky" in res.failed[0][2]
        assert "인자로 그대로 넘어가" in res.failed[0][2]
        new_text = res.workspace.read("pkg/mod.py")
        assert "ok_set = frozenset(ok)" in new_text
        assert "risky_set" not in new_text  # unsafe 쪽은 손대지 않음
        compile(new_text, "pkg/mod.py", "exec")
    finally:
        res.workspace.cleanup()


def test_no_candidates_leaves_before_after_zero(tmp_path):
    _mk(
        tmp_path,
        """
        def f(xs):
            seen = set()
            for x in xs:
                if x in seen:
                    pass
        """,
    )
    res = run_std_fix(cfg_for(tmp_path))
    try:
        assert res.before == 0
        assert res.after == 0
        assert not res.applied and not res.failed
    finally:
        res.workspace.cleanup()


def test_is_immutable_shaped_recognizes_sequence_tuple_frozenset():
    """탐지가 지금은 list 만 후보로 삼지만(파라미터가 Sequence/tuple 이면 애초에 후보가 안 됨),
    이 판정 함수 자체는 나중에 탐지 범위가 넓어져도 바로 쓸 수 있게 독립적으로 옳아야 한다."""
    import ast

    def ann_of(src: str):
        fn = ast.parse(src).body[0]
        assert isinstance(fn, ast.FunctionDef)
        return fn.args.args[0].annotation

    assert std_fix._is_immutable_shaped(ann_of("def f(x: Sequence): pass"))
    assert std_fix._is_immutable_shaped(ann_of("def f(x: tuple): pass"))
    assert std_fix._is_immutable_shaped(ann_of("def f(x: frozenset): pass"))
    assert not std_fix._is_immutable_shaped(ann_of("def f(x: list): pass"))
    assert not std_fix._is_immutable_shaped(None)
