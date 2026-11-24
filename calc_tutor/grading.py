"""Auto-grading for quiz answers.

Question types:
  mc         -- multiple choice; answer is the 0-based index of the right choice
  num        -- a number; learner may type 'pi/4', 'e^2', '1/3'. tol = relative tolerance
  expr       -- an expression in `var` (comma-separated for several); checked for
                symbolic/numeric equivalence, so '2x+2' == '2(x+1)'
  antideriv  -- an antiderivative; '+ C' optional; graded by differentiating
  free       -- free response, graded by the assistant (or self-graded in the UI)
"""

from __future__ import annotations

import json
import math
import random
import re

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

_TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
_FORBIDDEN = re.compile(r"__|[;'\"\[\]{}\\@`!:$#&|~]|lambda|import|exec|eval|open|getattr")
MAX_LEN = 200


class GradeError(ValueError):
    pass


def _locals(var_names: list[str]) -> dict:
    d = {
        "e": sp.E, "pi": sp.pi, "π": sp.pi, "ln": sp.log, "log": sp.log, "sqrt": sp.sqrt,
        "exp": sp.exp, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "sec": sp.sec,
        "csc": sp.csc, "cot": sp.cot, "arcsin": sp.asin, "arccos": sp.acos,
        "arctan": sp.atan, "asin": sp.asin, "acos": sp.acos, "atan": sp.atan,
        "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "abs": sp.Abs, "inf": sp.oo,
        "oo": sp.oo, "infinity": sp.oo, "C": sp.Symbol("C"),
    }
    if "i" not in var_names:
        d["i"] = sp.I  # imaginary unit; answers like 3+4i, (2-i)/5
    for v in var_names:
        d[v] = sp.Symbol(v, real=True)
    return d


def _split_words(text: str, vocab: set[str]) -> str:
    """'xln(x)' -> 'x ln(x)', 'xe^x' -> 'x e^x': segment letter runs into known names
    (longest match first) so implicit multiplication sees separate tokens."""
    def seg(word: str) -> list[str] | None:
        if not word:
            return []
        for n in range(len(word), 0, -1):
            if word[:n] in vocab:
                rest = seg(word[n:])
                if rest is not None:
                    return [word[:n], *rest]
        return None
    return re.sub(r"[A-Za-z]+", lambda m: " ".join(seg(m.group()) or [m.group()]), text)


def parse(text: str, var_names: list[str] | None = None) -> sp.Expr:
    text = (text or "").strip().strip("$").strip()
    if not text:
        raise GradeError("empty answer")
    if len(text) > MAX_LEN or _FORBIDDEN.search(text):
        raise GradeError("couldn't read that answer -- use plain math like 3x^2 + 2")
    text = text.replace("−", "-").replace("·", "*").replace("×", "*").replace("²", "^2")
    text = text.replace("³", "^3").replace("√", "sqrt")
    # 'y =' / 'f(x) =' / "dy/dx =" prefixes are noise
    text = re.sub(r"^\s*[\w()/' ]{1,10}=\s*", "", text) if "=" in text else text
    local = _locals(var_names or [])
    text = _split_words(text, {k for k in local if k.isalpha()})
    try:
        return parse_expr(text, local_dict=local, transformations=_TRANSFORMS)
    except Exception as e:  # sympy raises a zoo of types here
        raise GradeError(f"couldn't parse '{text}'") from e


def _numeric_equal(a: sp.Expr, b: sp.Expr, syms: list[sp.Symbol], trials: int = 8) -> bool:
    if not syms:
        try:
            av, bv = complex(sp.N(a)), complex(sp.N(b))
        except (TypeError, ValueError):
            return False
        return abs(av - bv) <= 1e-6 * max(1.0, abs(bv))
    rng = random.Random(42)
    fa, fb = sp.lambdify(syms, a, "math"), sp.lambdify(syms, b, "math")
    good = 0
    for _ in range(trials * 4):
        pt = [rng.uniform(0.3, 2.7) for _ in syms]
        try:
            va, vb = complex(fa(*pt)), complex(fb(*pt))
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            continue
        if any(math.isnan(z.real) or math.isinf(z.real) for z in (va, vb)):
            continue
        if abs(va - vb) > 1e-6 * max(1.0, abs(vb)):
            return False
        good += 1
        if good >= trials:
            return True
    return good > 0


def equivalent(given: str, key: str, var_names: list[str]) -> bool:
    g, k = parse(given, var_names), parse(key, var_names)
    syms = [sp.Symbol(v, real=True) for v in var_names]
    extra = (g.free_symbols - set(syms)) - {sp.Symbol("C")}
    if extra:
        raise GradeError(f"unexpected variable(s): {', '.join(sorted(map(str, extra)))}")
    return _numeric_equal(g, k, syms)


def check(q: dict, given: str) -> dict:
    """Returns {"correct": bool|None, "feedback": str}. None means needs a human or the assistant."""
    qtype = q["type"]
    key = q["answer"]
    try:
        if qtype == "mc":
            choices = json.loads(q["choices"]) if isinstance(q["choices"], str) else q["choices"]
            g = str(given).strip()
            idx = int(g) if g.isdigit() else next(
                (i for i, c in enumerate(choices) if c.strip().lower() == g.lower()), -1)
            return {"correct": idx == int(key), "feedback": ""}
        if qtype == "num":
            g, k = parse(given), parse(key)
            try:
                gv, kv = complex(sp.N(g)), complex(sp.N(k))
            except TypeError as e:
                raise GradeError("that doesn't evaluate to a number") from e
            tol = q.get("tol") or 1e-3
            ok = abs(gv - kv) <= tol * max(1.0, abs(kv))
            shown = f"{gv.real:.6g}" if abs(gv.imag) < 1e-12 else f"{gv.real:.4g}{gv.imag:+.4g}i"
            return {"correct": ok, "feedback": "" if ok else f"you gave ≈ {shown}"}
        vars_ = [v.strip() for v in (q.get("var") or "x").split(",") if v.strip()]
        if qtype == "expr":
            return {"correct": equivalent(given, key, vars_), "feedback": ""}
        if qtype == "antideriv":
            syms = [sp.Symbol(v, real=True) for v in vars_]
            g = parse(given, vars_).subs(sp.Symbol("C"), 0)
            k = parse(key, vars_).subs(sp.Symbol("C"), 0)
            ok = _numeric_equal(sp.diff(g, syms[0]), sp.diff(k, syms[0]), syms)
            return {"correct": ok, "feedback": ""}
        return {"correct": None, "feedback": "free response -- needs grading"}
    except GradeError as e:
        return {"correct": False, "feedback": str(e), "unparseable": True}


# --- CAS sandbox: verify AI-written questions and check working steps -----------------
#
# `verify` strings are plain sympy calls such as  diff(x**2*sin(x), x),
# integrate(exp(-x), (x, 0, oo)), limit(sin(x)/x, x, 0), summation(k**2, (k, 1, 4)),
# Matrix([[2,1],[3,4]]).det(). They run in a separate worker process with a timeout so a
# runaway integral can't hang the server, and through the same character/word filter as answers.

import concurrent.futures as _cf
import concurrent.futures.process  # noqa: F401  (BrokenProcessPool)
import multiprocessing as _mp

CAS_TIMEOUT = 15
_pool: _cf.ProcessPoolExecutor | None = None
_CAS_FORBIDDEN = re.compile(r"__|[;'\"\\@`!$#&|~]|lambda|import|exec|eval|open|getattr|"
                            r"globals|locals|compile|system|subprocess|os\.|sys\.")


def _cas_names() -> dict:
    names = {k: getattr(sp, k) for k in (
        "diff", "integrate", "limit", "summation", "Sum", "series", "simplify", "expand",
        "factor", "solve", "Matrix", "Rational", "sqrt", "exp", "log", "sin", "cos", "tan",
        "asin", "acos", "atan", "sinh", "cosh", "tanh", "Abs", "factorial", "binomial", "pi",
        "E", "oo", "Integer", "Float", "nsimplify", "det", "trace", "erf")}
    names.update({"ln": sp.log, "e": sp.E, "inf": sp.oo})
    for v in "abcdhkmnpqrstuvwxyz":
        names[v] = sp.Symbol(v, real=True)
    names["n"] = sp.Symbol("n", integer=True, positive=True)
    names["k"] = sp.Symbol("k", integer=True)
    return names


def _cas_worker(text: str) -> str:
    expr = parse_expr(text, local_dict=_cas_names(), transformations=standard_transformations)
    if hasattr(expr, "doit"):
        expr = expr.doit()
    return sp.srepr(sp.nsimplify(expr) if expr.is_number and not expr.is_Integer else expr)


def cas_eval(text: str) -> sp.Expr:
    """Evaluate a sympy expression string in the sandbox worker. Raises GradeError."""
    global _pool
    text = (text or "").strip()
    if not text or len(text) > 400 or _CAS_FORBIDDEN.search(text):
        raise GradeError("verify expression rejected (empty, too long, or disallowed tokens)")
    if _pool is None:
        _pool = _cf.ProcessPoolExecutor(max_workers=1, mp_context=_mp.get_context("spawn"))
    try:
        out = _pool.submit(_cas_worker, text).result(timeout=CAS_TIMEOUT)
    except _cf.TimeoutError as e:
        _pool.shutdown(wait=False, cancel_futures=True)
        for p in list(getattr(_pool, "_processes", {}).values()):
            p.kill()
        _pool = None
        raise GradeError(f"CAS timed out after {CAS_TIMEOUT}s") from e
    except _cf.process.BrokenProcessPool as e:
        _pool = None  # a crashed worker poisons the pool; start fresh next call
        raise GradeError("CAS worker crashed; try again") from e
    except Exception as e:  # sympy/parse errors come back pickled
        raise GradeError(f"CAS could not evaluate: {e}") from e
    return sp.sympify(out)  # srepr from our own worker round-trips exactly


def verify_question(qtype: str, answer: str, verify: str, var: str | None) -> dict:
    """Check that `answer` matches what the CAS computes from `verify`.
    expr/num: verify computes the answer. antideriv: verify is the integrand."""
    vars_ = [v.strip() for v in (var or "x").split(",") if v.strip()]
    truth = cas_eval(verify)
    ans = parse(answer, vars_)
    syms = [sp.Symbol(v, real=True) for v in vars_]
    # CAS symbols carry assumptions that may differ; unify by name
    truth = truth.subs({s: sp.Symbol(s.name, real=True) for s in truth.free_symbols})
    if qtype == "antideriv":
        ok = _numeric_equal(sp.diff(ans.subs(sp.Symbol("C"), 0), syms[0]), truth, syms)
    elif qtype == "num":
        ok = _numeric_equal(ans, truth, [])
    else:
        ok = _numeric_equal(ans, truth, syms)
    return {"verified": bool(ok), "cas_value": str(truth)}


def steps_equivalent(before: str, after: str, var: str | None = "x") -> dict:
    """Is `after` a valid rewrite of `before`? (symbolic/numeric equivalence)."""
    vars_ = [v.strip() for v in (var or "x").split(",") if v.strip()]
    try:
        ok = equivalent(after, before, vars_)
    except GradeError as e:
        return {"equivalent": None, "error": str(e)}
    return {"equivalent": bool(ok)}
