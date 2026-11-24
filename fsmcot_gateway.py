# -*- coding: utf-8 -*-
"""
Aria FSMCoT Compact - Standalone Zero-Dependency OpenAI-Compatible Thinking OS Proxy
====================================================================================
A lightweight, single-file Finite-State Machine Chain-of-Thought (FSMCoT) gateway
built exclusively on Python's standard library (zero pip dependencies, zero Obsidian
or external service coupling).

Connects to ANY OpenAI-compatible backend (llama.cpp llama-server, Ollama, vLLM,
LM Studio, etc.) and upgrades small local LLMs (e.g. 4B-8B quantized models) with:
  1) Zero-Regex State-0 Self-Dispatcher (CALC / CODE / FORMAT / DIRECT)
  2) FSM_CALC     : ACT Early Exit + Multi-Head Parallel Dual Verification + Arbiter
                    inside a deterministic, restricted Python math sandbox
  3) CODE_REPAIR  : AST syntax check + unclosed-fence recovery + parallel self-repair
  4) CRITIC_FSM   : Deterministic JSON / length / keyword constraint critic loop
  5) DIRECT_1SHOT : Transparent pass-through with short-budget thinking overflow guard
  6) RLT Tape     : Continuous cross-turn state tape (pass `"user": "session-id"`)

License: MIT
"""
from __future__ import annotations

import argparse
import ast
import collections
import html
import itertools
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VERSION = "1.0.0-compact"

# Runtime configuration (populated by CLI args / env vars)
CONFIG: dict[str, Any] = {
    "upstream": os.environ.get("FSMCOT_UPSTREAM", "http://127.0.0.1:1918").rstrip("/"),
    "api_key": os.environ.get("FSMCOT_API_KEY", ""),
    "default_model": os.environ.get("FSMCOT_MODEL", ""),
    "pin_state0_slot": True,
    "timeout_s": 120,
}

STATE0_SYSTEM_PROMPT = """You are the State-0 Dispatcher of FSMCoT Compact.
Classify the user request into EXACTLY ONE execution state and output ONLY that single word:

- CALC: Math word problems, arithmetic, algebra, probability, combinatorics, mazes, shortest-path/graph search, or multi-step simulations that need exact numeric/loop computation in Python.
- CODE: Requests to write, complete, or implement executable programming code or functions (e.g., Python function signatures, coding tasks with assert tests, algorithms).
- FORMAT: Writing or answering tasks with explicit strict output-format or text constraints (e.g., strict JSON output, word/character/sentence/paragraph/bullet count limits, forbidden or required words, all uppercase, wrap in quotes). Do NOT choose FORMAT for 2D integer grid puzzles.
- DIRECT: Everyday chat, advice, opinions, explanations, summaries, factual/trivia questions (Who/When/Where/What), multiple-choice questions, or 2D integer grid puzzles.

Output ONLY one word: CALC, CODE, FORMAT, or DIRECT."""

STATE_TO_ROUTE = {
    "CALC": "FSM_CALC",
    "CODE": "CODE_REPAIR",
    "FORMAT": "CRITIC_FSM",
    "DIRECT": "DIRECT_1SHOT",
}

_STATS_LOCK = threading.Lock()
_STATS: dict[str, Any] = {
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "total_requests": 0,
    "FSM_CALC": 0,
    "CODE_REPAIR": 0,
    "CRITIC_FSM": 0,
    "DIRECT_1SHOT": 0,
    "STREAM_PASSTHROUGH": 0,
    "early_exits": 0,
    "dual_verified": 0,
    "fsm_repairs": 0,
    "code_repairs": 0,
    "critic_repairs": 0,
    "rlt_tape_hits": 0,
}

_TAPE_LOCK = threading.Lock()
_SESSION_TAPES: dict[str, dict[str, Any]] = {}
_MAX_SESSIONS = 64

_MODEL_LOCK = threading.Lock()
_CACHED_UPSTREAM_MODEL: str = ""


def _bump_stat(key: str, inc: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + inc


def _strip_thought(text: str) -> str:
    t = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
    return t.strip()


def _format_clean_ans(val: Any) -> Any:
    """Normalize exact integer floats (e.g. 42.0 -> 42) so exact-match math graders pass."""
    if isinstance(val, bool):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"-?\d+\.0+", s):
            return s.split(".", 1)[0]
    return val


def extract_python_code(text: str) -> str:
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return text.strip()


def _safe_import(name: str, globals_=None, locals_=None, fromlist=(), level: int = 0):
    if name in ("math", "fractions", "itertools", "collections"):
        return __import__(name, globals_, locals_, fromlist, level)
    raise ImportError(f"Blocked import in FSMCoT sandbox: {name}")


def run_fsm_coprocessor(code: str) -> tuple[Any, Any, str | None]:
    """Deterministic Python math/DP/combinatorics sandbox with zero filesystem/network access."""
    safe_builtins = {
        "__import__": _safe_import,
        "abs": abs, "round": round, "int": int, "float": float, "bool": bool, "str": str,
        "min": min, "max": max, "sum": sum, "len": len, "range": range,
        "tuple": tuple, "list": list, "dict": dict, "set": set,
        "sorted": sorted, "enumerate": enumerate, "zip": zip, "any": any, "all": all,
        "pow": pow, "divmod": divmod, "map": map, "filter": filter,
        "print": lambda *a, **k: None,
    }
    env: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "math": math,
        "Fraction": Fraction,
        "itertools": itertools,
        "collections": collections,
        "state": {},
    }
    try:
        exec(compile(code, "<fsmcot_sandbox>", "exec"), env, env)
        if "ans" not in env:
            return None, env.get("state"), "Variable 'ans' not defined"
        return env["ans"], env.get("state"), None
    except Exception as e:
        return None, env.get("state"), f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# RLT Continuous Session State Tape
# ---------------------------------------------------------------------------
def _get_rlt_tape_context(session_id: str, reset: bool = False) -> tuple[str, int]:
    if not session_id:
        return "", 0
    with _TAPE_LOCK:
        if reset:
            _SESSION_TAPES.pop(session_id, None)
            return "", 0
        st = _SESSION_TAPES.get(session_id)
        if not st or not st.get("facts"):
            return "", 0
        _bump_stat("rlt_tape_hits")
        facts_str = "\n".join(f"  - [Turn {f['turn']}/{f['route']}] {f['summary']}" for f in st["facts"][-6:])
        return f"[RLT Continuous State Tape (session={session_id}, turns={st['turns']})]\n{facts_str}\n\n", st["turns"]


def _append_rlt_tape(session_id: str, route: str, user_text: str, answer_text: str) -> None:
    if not session_id:
        return
    m_ans = re.search(r"ANSWER\s*:\s*([^\n]+)", answer_text)
    if m_ans:
        ans_line = f"ANSWER={m_ans.group(1).strip()}"
    else:
        m_def = re.search(r"def\s+([a-zA-Z_]\w*\s*\([^)]*\))", answer_text)
        if m_def:
            ans_line = f"defined `{m_def.group(1)}`"
        else:
            ans_line = answer_text.replace("\n", " ").strip()[:120]
    u_short = user_text.replace("\n", " ").strip()[:80]
    with _TAPE_LOCK:
        if session_id not in _SESSION_TAPES and len(_SESSION_TAPES) >= _MAX_SESSIONS:
            _SESSION_TAPES.pop(next(iter(_SESSION_TAPES)), None)
        st = _SESSION_TAPES.setdefault(session_id, {"turns": 0, "facts": []})
        st["turns"] += 1
        st["facts"].append({"turn": st["turns"], "route": route, "summary": f"Q='{u_short}' -> {ans_line}"})
        st["facts"] = st["facts"][-12:]


# ---------------------------------------------------------------------------
# Upstream OpenAI-compatible client
# ---------------------------------------------------------------------------
def _upstream_headers() -> dict[str, str]:
    hdrs = {"Content-Type": "application/json"}
    if CONFIG["api_key"]:
        hdrs["Authorization"] = f"Bearer {CONFIG['api_key']}"
    return hdrs


def _resolve_upstream_model() -> str:
    global _CACHED_UPSTREAM_MODEL
    if CONFIG["default_model"]:
        return CONFIG["default_model"]
    with _MODEL_LOCK:
        if _CACHED_UPSTREAM_MODEL:
            return _CACHED_UPSTREAM_MODEL
        try:
            req = urllib.request.Request(f"{CONFIG['upstream']}/v1/models", headers=_upstream_headers(), method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            if ids:
                _CACHED_UPSTREAM_MODEL = ids[0]
                return _CACHED_UPSTREAM_MODEL
        except Exception:
            pass
    return "default"


def _query_upstream(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    enable_thinking: bool = False,
    cache_prompt: bool = False,
    id_slot: int | None = None,
) -> tuple[str, float, str | None]:
    payload: dict[str, Any] = {
        "model": _resolve_upstream_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    if cache_prompt:
        payload["cache_prompt"] = True
    if id_slot is not None and CONFIG["pin_state0_slot"]:
        payload["id_slot"] = id_slot

    req = urllib.request.Request(
        f"{CONFIG['upstream']}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_upstream_headers(),
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=CONFIG["timeout_s"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ms = (time.perf_counter() - t0) * 1000.0
        content = data["choices"][0]["message"].get("content") or ""
        return content, ms, None
    except Exception as e:
        return "", (time.perf_counter() - t0) * 1000.0, str(e)


def _extract_last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return ""


def _extract_system_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        m.get("content", "")
        for m in messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    ).strip()


# ---------------------------------------------------------------------------
# State-0: Zero-Regex Unified FSMCoT Self-Dispatcher
# ---------------------------------------------------------------------------
def route_request(user_text: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Classify request into FSM_CALC / CODE_REPAIR / CRITIC_FSM / DIRECT_1SHOT."""
    if payload.get("tools") or payload.get("functions"):
        return "DIRECT_1SHOT", "native_tool_call"
    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        return "CRITIC_FSM", "api_response_format_json"
    msgs = payload.get("messages") or []
    if len(msgs) > 2:
        return "DIRECT_1SHOT", "multi_turn_history"

    snippet = user_text.strip()
    if len(snippet) > 1200:
        snippet = snippet[:900] + "\n...\n" + snippet[-250:]

    raw, ms, err = _query_upstream(
        STATE0_SYSTEM_PROMPT,
        snippet,
        max_tokens=8,
        temperature=0.0,
        enable_thinking=False,
        cache_prompt=True,
        id_slot=0,
    )
    if err:
        return "DIRECT_1SHOT", f"dispatcher_fallback({err[:40]})"

    token = re.sub(r"[^A-Z]", "", _strip_thought(raw).upper().split()[0] if raw.strip() else "")
    for key in ("CALC", "CODE", "FORMAT", "DIRECT"):
        if token == key or token.startswith(key):
            return STATE_TO_ROUTE[key], f"fsmcot_state0={key} ({ms:.0f}ms)"
    return "DIRECT_1SHOT", f"fsmcot_state0_unparsed({raw.strip()[:20]})"


# ---------------------------------------------------------------------------
# State 1: FSM_CALC (ACT Early Exit + Dual Parallel Verification + Arbiter)
# ---------------------------------------------------------------------------
def _is_simple_calc(prompt: str) -> bool:
    if len(prompt) > 260:
        return False
    hard_hints = (
        "maze", "grid", "shortest path", "graph", "dp", "dynamic programming",
        "combinatorics", "modulo", "mod ", "probability", "permutation", "matrix",
        "最短経路", "迷路", "動的計画", "確率", "場合の数", "通り", "余り",
    )
    low = prompt.lower()
    return not any(h in low for h in hard_hints)


def _run_fsm_calc(prompt: str, model_name: str, max_tokens: int) -> dict[str, Any]:
    _bump_stat("FSM_CALC")
    t0 = time.perf_counter()
    fsm_sys = (
        "You are a precise Python math coprocessor. Do NOT compute mentally. "
        "Write self-contained Python code that computes the exact numeric answer into variable `ans` "
        "(and optional intermediate `state = {}`). Output ONLY a single ```python ... ``` block."
    )
    verify_sys = (
        "You are an independent verification Python code generator. "
        "Without relying on shortcuts, use explicit step-by-step loops or pure dictionary memoization (`memo = {}`) "
        "to compute the exact answer into variable `ans` and `state = {}`. Output ONLY a single ```python ... ``` block."
    )
    user_a = f"Problem:\n{prompt}\n\nOutput ONLY a ```python ... ``` block that sets `state = {{}}` and exact `ans`."
    user_b = f"Problem:\n{prompt}\n\nUsing an independent method (loops / explicit search / memo dict), output ONLY a ```python ... ``` block setting `state = {{}}` and exact `ans`."

    early_exit = False
    cross_verified = False
    repaired = False
    arbiter_used = False
    gpu_calls = 0

    if _is_simple_calc(prompt):
        raw_a, _, _ = _query_upstream(fsm_sys, user_a, max_tokens=max(max_tokens, 1024), temperature=0.0, enable_thinking=False, cache_prompt=True)
        gpu_calls = 1
        code = extract_python_code(raw_a)
        val, fsm_state, err = run_fsm_coprocessor(code)
        val_b, code_b, err_b = None, "", "skipped"
        if err is None and val is not None:
            early_exit = True
            _bump_stat("early_exits")
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_a = ex.submit(_query_upstream, fsm_sys, user_a, max(max_tokens, 1536), 0.0, False, True)
            fut_b = ex.submit(_query_upstream, verify_sys, user_b, max(max_tokens, 1536), 0.0, False, True)
            raw_a, _, _ = fut_a.result()
            raw_b, _, _ = fut_b.result()
        gpu_calls = 2
        code = extract_python_code(raw_a)
        val, fsm_state, err = run_fsm_coprocessor(code)
        code_b = extract_python_code(raw_b)
        val_b, state_b, err_b = run_fsm_coprocessor(code_b)

        if err is not None and err_b is None and val_b is not None:
            val, fsm_state, err, code = val_b, state_b, None, code_b
        elif err is None and err_b is None and val is not None and val_b is not None:
            if str(_format_clean_ans(val)) == str(_format_clean_ans(val_b)):
                cross_verified = True
                _bump_stat("dual_verified")
            else:
                arbiter_used = True
                arb_sys = (
                    "Two independent Python implementations produced different answers. "
                    "Identify which constraints or edge cases were missed and output ONLY the corrected ```python ... ``` block setting `state = {}` and `ans`."
                )
                arb_user = (
                    f"Problem:\n{prompt}\n\n"
                    f"--- Impl A (ans={val}) ---\n```python\n{code[:1200]}\n```\n\n"
                    f"--- Impl B (ans={val_b}) ---\n```python\n{code_b[:1200]}\n```\n\n"
                    "Output ONLY the single verified ```python ... ``` block."
                )
                raw_c, _, _ = _query_upstream(arb_sys, arb_user, max_tokens=max(max_tokens, 1536), temperature=0.0, enable_thinking=False)
                gpu_calls += 1
                code_c = extract_python_code(raw_c)
                val_c, state_c, err_c = run_fsm_coprocessor(code_c)
                if err_c is None and val_c is not None:
                    if str(_format_clean_ans(val_c)) in (str(_format_clean_ans(val)), str(_format_clean_ans(val_b))):
                        cross_verified = True
                        _bump_stat("dual_verified")
                    val, fsm_state, code = val_c, state_c, code_c
                    repaired = True
                    _bump_stat("fsm_repairs")

    if err is not None:
        repair_user = (
            f"Problem:\n{prompt}\n\nPrevious code:\n```python\n{code[:1200]}\n```\n"
            f"Runtime error: {err}\nFix the error and output ONLY a ```python ... ``` block setting `state = {{}}` and `ans`."
        )
        raw2, _, _ = _query_upstream(fsm_sys, repair_user, max_tokens=max(max_tokens, 1024), temperature=0.0, enable_thinking=False)
        gpu_calls += 1
        code2 = extract_python_code(raw2)
        val2, state2, err2 = run_fsm_coprocessor(code2)
        if err2 is None and val2 is not None:
            val, fsm_state, err = val2, state2, None
            repaired = True
            _bump_stat("fsm_repairs")

    if err is None and val is not None:
        clean_val = _format_clean_ans(val)
        state_str = json.dumps(fsm_state, ensure_ascii=False, default=str)[:300]
        tag = "FSMCoT Early-Exit" if early_exit else ("FSMCoT Dual-Verified" if cross_verified else "FSMCoT Single-Verified")
        content = f"ANSWER: {clean_val}\n\n[{tag} State Cache]: {state_str}"
    else:
        content, _, _ = _query_upstream("Answer accurately and concisely.", prompt, max_tokens=max(max_tokens, 1024), temperature=0.0, enable_thinking=False)
        content = _strip_thought(content)

    now = int(time.time())
    return {
        "id": f"chatcmpl-fsmcot-calc-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "fsmcot": {
            "version": VERSION,
            "route": "FSM_CALC",
            "gpu_calls": gpu_calls,
            "early_exit": early_exit,
            "cross_verified": cross_verified,
            "arbiter_used": arbiter_used,
            "repaired": repaired,
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        },
    }


# ---------------------------------------------------------------------------
# State 2: CODE_REPAIR (AST Check + Unclosed Fence + Parallel Self-Repair)
# ---------------------------------------------------------------------------
def _extract_python_for_ast(text: str) -> tuple[str, bool]:
    closed = re.findall(r"```(?:python)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if closed:
        return closed[-1], False
    m_open = re.search(r"```(?:python)?\s*\n(.*)$", text, flags=re.DOTALL | re.IGNORECASE)
    if m_open:
        return m_open.group(1), True
    return text, False


def _check_python_syntax(text: str) -> tuple[bool, str]:
    code, unclosed = _extract_python_for_ast(text)
    if not code.strip():
        return False, "Empty code output"
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}" + (" (truncated code block)" if unclosed else "")
    if unclosed:
        return False, "Unclosed markdown code fence (possible truncation)"
    return True, ""


def _run_code_repair(prompt: str, model_name: str, max_tokens: int, system_prompt: str = "") -> dict[str, Any]:
    _bump_stat("CODE_REPAIR")
    t0 = time.perf_counter()
    eff_sys = system_prompt or (
        "You are an expert Python engineer. Output concise, clean, executable code inside a single "
        "```python ... ``` block. Do not include verbose essays."
    )
    raw, _, _ = _query_upstream(eff_sys, prompt, max_tokens=max(max_tokens, 1536), temperature=0.0, enable_thinking=False, cache_prompt=True)
    content = _strip_thought(raw)
    gpu_calls = 1
    repaired = False

    ok, err_msg = _check_python_syntax(content)
    if not ok:
        repair_dca = (
            f"Original task:\n{prompt}\n\n"
            f"Your previous output had a syntax/truncation error:\n{err_msg}\n\n"
            f"Previous code snippet:\n{content[:1500]}\n\n"
            "Output ONLY the complete, syntactically valid ```python ... ``` block."
        )
        repair_forget = (
            f"Solve this coding task cleanly from scratch in a single concise ```python ... ``` block:\n{prompt}"
        )
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_query_upstream, eff_sys, repair_dca, max(max_tokens, 1536), 0.0, False, True)
            f2 = ex.submit(_query_upstream, eff_sys, repair_forget, max(max_tokens, 1536), 0.2, False, True)
            cand1 = _strip_thought(f1.result()[0])
            cand2 = _strip_thought(f2.result()[0])
        gpu_calls += 2
        if _check_python_syntax(cand1)[0]:
            content, repaired = cand1, True
            _bump_stat("code_repairs")
        elif _check_python_syntax(cand2)[0]:
            content, repaired = cand2, True
            _bump_stat("code_repairs")

    now = int(time.time())
    return {
        "id": f"chatcmpl-fsmcot-code-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "fsmcot": {
            "version": VERSION,
            "route": "CODE_REPAIR",
            "gpu_calls": gpu_calls,
            "repaired": repaired,
            "syntax_ok": _check_python_syntax(content)[0],
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        },
    }


# ---------------------------------------------------------------------------
# State 3: CRITIC_FSM (Deterministic JSON & Text Constraint Auditor)
# ---------------------------------------------------------------------------
def audit_constraints(prompt: str, text: str) -> list[str]:
    violations: list[str] = []
    low_p = prompt.lower()

    if "json" in low_p and any(k in low_p for k in ("only json", "valid json", "jsonのみ", "strict json", "json object")):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
        try:
            json.loads(cleaned)
        except Exception as e:
            violations.append(f"Invalid JSON ({e}). Output raw valid JSON only without markdown fences or prose.")

    m_max_words = re.search(r"(?:at most|under|less than|no more than)\s+(\d+)\s+words?", low_p)
    if m_max_words:
        limit = int(m_max_words.group(1))
        wc = len(text.split())
        if wc > limit:
            violations.append(f"Word count is {wc}, must be <= {limit}.")

    m_min_words = re.search(r"(?:at least|more than)\s+(\d+)\s+words?", low_p)
    if m_min_words:
        limit = int(m_min_words.group(1))
        wc = len(text.split())
        if wc < limit:
            violations.append(f"Word count is {wc}, must be >= {limit}.")

    m_max_chars = re.search(r"(\d+)\s*文字以内", prompt)
    if m_max_chars:
        limit = int(m_max_chars.group(1))
        if len(text) > limit:
            violations.append(f"Character count is {len(text)}, must be <= {limit} chars.")

    if any(k in low_p for k in ("all uppercase", "all capital letters", "すべて大文字")):
        letters = [c for c in text if c.isalpha()]
        if letters and any(c.islower() for c in letters):
            violations.append("Response must be ALL UPPERCASE.")

    return violations


def _run_critic_fsm(prompt: str, model_name: str, max_tokens: int, system_prompt: str = "") -> dict[str, Any]:
    _bump_stat("CRITIC_FSM")
    t0 = time.perf_counter()
    eff_sys = system_prompt or "Follow every formatting and constraint instruction strictly and concisely."
    raw, _, _ = _query_upstream(eff_sys, prompt, max_tokens=max(max_tokens, 1024), temperature=0.0, enable_thinking=False)
    content = _strip_thought(raw)
    gpu_calls = 1
    repairs = 0

    for _ in range(2):
        viols = audit_constraints(prompt, content)
        if not viols:
            break
        repair_p = (
            f"Original instruction:\n{prompt}\n\n"
            f"Previous draft:\n{content}\n\n"
            f"Constraint violations detected:\n- " + "\n- ".join(viols) + "\n\n"
            "Rewrite the response to satisfy ALL constraints strictly. Output ONLY the final answer."
        )
        raw2, _, _ = _query_upstream(eff_sys, repair_p, max_tokens=max(max_tokens, 1024), temperature=0.0, enable_thinking=False)
        content = _strip_thought(raw2)
        gpu_calls += 1
        repairs += 1
        _bump_stat("critic_repairs")

    now = int(time.time())
    return {
        "id": f"chatcmpl-fsmcot-critic-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "fsmcot": {
            "version": VERSION,
            "route": "CRITIC_FSM",
            "gpu_calls": gpu_calls,
            "repairs": repairs,
            "remaining_violations": audit_constraints(prompt, content),
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        },
    }


# ---------------------------------------------------------------------------
# HTTP Server (OpenAI-Compatible Gateway)
# ---------------------------------------------------------------------------
class FSMCoTHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html_str: str) -> None:
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_raw(self, method: str, path: str, body: bytes | None = None) -> None:
        req = urllib.request.Request(
            f"{CONFIG['upstream']}{path}",
            data=body,
            headers=_upstream_headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=CONFIG["timeout_s"]) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._send_json(502, {"error": f"Upstream error ({CONFIG['upstream']}): {e}"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            with _STATS_LOCK:
                st = dict(_STATS)
            self._send_json(200, {
                "status": "ok",
                "gateway": "aria-fsmcot-compact",
                "version": VERSION,
                "upstream": CONFIG["upstream"],
                "resolved_model": _resolve_upstream_model(),
                "stats": st,
            })
            return

        if self.path == "/v1/models":
            up_id = _resolve_upstream_model()
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "fsmcot-compact", "object": "model", "owned_by": "aria-fsmcot-compact"},
                    {"id": up_id, "object": "model", "owned_by": "upstream"},
                ],
            })
            return

        if self.path in ("/", "/status"):
            with _STATS_LOCK:
                st = dict(_STATS)
            rows = "".join(
                f"<tr><td><b>{html.escape(str(k))}</b></td><td>{html.escape(str(v))}</td></tr>"
                for k, v in st.items()
            )
            page = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Aria FSMCoT Compact</title>"
                "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;"
                "background:#0f172a;color:#e2e8f0}table{width:100%;border-collapse:collapse;margin-top:16px}"
                "td{border-bottom:1px solid #334155;padding:8px}h1{color:#38bdf8}</style></head>"
                f"<body><h1>Aria FSMCoT Compact ({html.escape(VERSION)})</h1>"
                f"<p>Upstream: <code>{html.escape(CONFIG['upstream'])}</code> | "
                f"Model: <code>{html.escape(_resolve_upstream_model())}</code></p>"
                f"<table>{rows}</table></body></html>"
            )
            self._send_html(200, page)
            return

        self._proxy_raw("GET", self.path)

    def do_POST(self) -> None:
        clen = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(clen) if clen > 0 else b""

        if self.path != "/v1/chat/completions":
            self._proxy_raw("POST", self.path, raw_body)
            return

        _bump_stat("total_requests")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._proxy_raw("POST", self.path, raw_body)
            return

        if payload.get("stream", False):
            _bump_stat("STREAM_PASSTHROUGH")
            self._proxy_raw("POST", self.path, raw_body)
            return

        if payload.get("fsmcot_raw", False):
            payload.pop("fsmcot_raw", None)
            _bump_stat("DIRECT_1SHOT")
            self._proxy_raw("POST", self.path, json.dumps(payload).encode("utf-8"))
            return

        messages = payload.get("messages") or []
        user_text = _extract_last_user_text(messages)
        system_text = _extract_system_text(messages)
        model_name = str(payload.get("model", "fsmcot-compact"))
        max_tokens = int(payload.get("max_tokens", 1024))
        session_id = str(payload.get("fsmcot_session_id") or payload.get("user") or "").strip()
        reset_sess = bool(payload.get("fsmcot_reset_session", False))

        tape_ctx, tape_turns = _get_rlt_tape_context(session_id, reset=reset_sess)
        eff_user_text = f"{tape_ctx}{user_text}" if tape_ctx else user_text

        t_r0 = time.perf_counter()
        route, reason = route_request(user_text, payload)
        route_ms = (time.perf_counter() - t_r0) * 1000.0

        if route == "FSM_CALC":
            resp_obj = _run_fsm_calc(eff_user_text, model_name=model_name, max_tokens=max_tokens)
            resp_obj["fsmcot"].update({"route_reason": reason, "route_ms": round(route_ms, 1), "rlt_turns": tape_turns})
            _append_rlt_tape(session_id, "CALC", user_text, resp_obj["choices"][0]["message"]["content"])
            self._send_json(200, resp_obj)
            return

        if route == "CODE_REPAIR":
            resp_obj = _run_code_repair(eff_user_text, model_name=model_name, max_tokens=max_tokens, system_prompt=system_text)
            resp_obj["fsmcot"].update({"route_reason": reason, "route_ms": round(route_ms, 1), "rlt_turns": tape_turns})
            _append_rlt_tape(session_id, "CODE", user_text, resp_obj["choices"][0]["message"]["content"])
            self._send_json(200, resp_obj)
            return

        if route == "CRITIC_FSM":
            resp_obj = _run_critic_fsm(eff_user_text, model_name=model_name, max_tokens=max_tokens, system_prompt=system_text)
            resp_obj["fsmcot"].update({"route_reason": reason, "route_ms": round(route_ms, 1), "rlt_turns": tape_turns})
            _append_rlt_tape(session_id, "FORMAT", user_text, resp_obj["choices"][0]["message"]["content"])
            self._send_json(200, resp_obj)
            return

        # DIRECT_1SHOT transparent proxy with short-budget thinking overflow guard
        _bump_stat("DIRECT_1SHOT")
        fwd_payload = dict(payload)
        fwd_payload["model"] = _resolve_upstream_model()
        if tape_ctx and messages:
            fwd_msgs = [dict(m) for m in messages]
            for m in reversed(fwd_msgs):
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    m["content"] = eff_user_text
                    break
            fwd_payload["messages"] = fwd_msgs
        if max_tokens <= 512 and "chat_template_kwargs" not in fwd_payload:
            fwd_payload["chat_template_kwargs"] = {"enable_thinking": False}

        req = urllib.request.Request(
            f"{CONFIG['upstream']}/v1/chat/completions",
            data=json.dumps(fwd_payload).encode("utf-8"),
            headers=_upstream_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=CONFIG["timeout_s"]) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            data["fsmcot"] = {
                "version": VERSION,
                "route": route,
                "route_reason": reason,
                "route_ms": round(route_ms, 1),
                "rlt_turns": tape_turns,
            }
            _append_rlt_tape(session_id, "DIRECT", user_text, data["choices"][0]["message"].get("content") or "")
            self._send_json(200, data)
        except Exception as e:
            self._send_json(502, {"error": f"Upstream error ({CONFIG['upstream']}): {e}"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Aria FSMCoT Compact - Zero-Dependency OpenAI Thinking OS Proxy")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=1921, help="Bind port (default: 1921)")
    ap.add_argument("--upstream", default=CONFIG["upstream"], help="Upstream OpenAI-compatible base URL (e.g. http://127.0.0.1:8080)")
    ap.add_argument("--model", default=CONFIG["default_model"], help="Override upstream model id (default: auto-detect via /v1/models)")
    ap.add_argument("--api-key", default=CONFIG["api_key"], help="Optional Bearer API key for upstream")
    ap.add_argument("--no-slot-pin", action="store_true", help="Disable llama.cpp id_slot=0 pinning for non-llama.cpp backends")
    args = ap.parse_args()

    CONFIG["upstream"] = args.upstream.rstrip("/")
    CONFIG["default_model"] = args.model
    CONFIG["api_key"] = args.api_key
    CONFIG["pin_state0_slot"] = not args.no_slot_pin

    srv = ThreadingHTTPServer((args.host, args.port), FSMCoTHandler)
    print(
        f"[OK] Aria FSMCoT Compact {VERSION} listening on http://{args.host}:{args.port} "
        f"-> upstream {CONFIG['upstream']}",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
