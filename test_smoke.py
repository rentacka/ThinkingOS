# -*- coding: utf-8 -*-
"""
Offline unit & sandbox safety smoke tests for Aria FSMCoT Compact.
Run: python test_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsmcot_gateway as gw


def test_clean_ans() -> None:
    assert gw._format_clean_ans(42.0) == 42
    assert gw._format_clean_ans("42.0") == "42"
    assert gw._format_clean_ans(3.14) == 3.14
    assert gw._format_clean_ans(True) is True


def test_sandbox_math() -> None:
    val, state, err = gw.run_fsm_coprocessor("state = {'a': 15*3, 'b': 12//4}\nans = state['a'] - state['b']")
    assert err is None
    assert val == 42
    assert state["a"] == 45


def test_sandbox_blocks_os() -> None:
    val, _, err = gw.run_fsm_coprocessor("import os\nans = os.listdir('.')")
    assert val is None
    assert err is not None and "Blocked import" in err


def test_syntax_checker() -> None:
    ok, _ = gw._check_python_syntax("```python\ndef add(a, b):\n    return a + b\n```")
    assert ok is True
    ok2, msg2 = gw._check_python_syntax("```python\ndef broken(a, b:\n    return a\n```")
    assert ok2 is False and "SyntaxError" in msg2
    ok3, msg3 = gw._check_python_syntax("```python\nx = 1\n")
    assert ok3 is False and "Unclosed" in msg3


def test_critic_auditor() -> None:
    assert gw.audit_constraints("Return valid JSON only.", '{"status": "ok", "code": 200}') == []
    assert len(gw.audit_constraints("Return valid JSON only.", "Here is json: {bad}")) == 1
    assert len(gw.audit_constraints("Answer in at most 3 words.", "one two three four")) == 1
    assert gw.audit_constraints("Answer in ALL UPPERCASE.", "HELLO WORLD") == []
    assert len(gw.audit_constraints("Answer in ALL UPPERCASE.", "Hello World")) == 1


def test_rlt_tape() -> None:
    gw._append_rlt_tape("sess-test", "CALC", "15*3-12/4", "ANSWER: 42")
    ctx, turns = gw._get_rlt_tape_context("sess-test")
    assert turns == 1 and "ANSWER=42" in ctx
    ctx2, turns2 = gw._get_rlt_tape_context("sess-test", reset=True)
    assert turns2 == 0 and ctx2 == ""


def main() -> None:
    tests = [
        test_clean_ans,
        test_sandbox_math,
        test_sandbox_blocks_os,
        test_syntax_checker,
        test_critic_auditor,
        test_rlt_tape,
    ]
    for fn in tests:
        fn()
        print(f"[PASS] {fn.__name__}")
    print(f"\nAll {len(tests)}/{len(tests)} offline smoke tests PASSED!")


if __name__ == "__main__":
    main()
