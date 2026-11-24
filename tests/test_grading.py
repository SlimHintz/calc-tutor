import json

import pytest

from calc_tutor import grading


def q(type_, answer, var="x", choices=None, tol=None):
    return {"type": type_, "answer": answer, "var": var, "tol": tol,
            "choices": json.dumps(choices) if choices else None}


@pytest.mark.parametrize("given", ["2x+2", "2(x+1)", "2*x + 2", "x + x + 2", "y = 2x+2",
                                   "$2x+2$", "2·x+2"])
def test_expr_equivalent_forms(given):
    assert grading.check(q("expr", "2*(x+1)"), given)["correct"] is True


@pytest.mark.parametrize("given", ["2x", "x^2+2", "2x+3"])
def test_expr_wrong(given):
    assert grading.check(q("expr", "2*(x+1)"), given)["correct"] is False


def test_expr_functions():
    assert grading.check(q("expr", "1/(x+1)^2"), "(x+1)^(-2)")["correct"]
    assert grading.check(q("expr", "3e^(3x)"), "3*exp(3x)")["correct"]
    assert grading.check(q("expr", "ln(7)/2"), "0.5 ln 7")["correct"]
    assert grading.check(q("expr", "x/sqrt(1+x^2)"), "x(1+x^2)^(-1/2)")["correct"]


@pytest.mark.parametrize("given,key", [("xe^(-x)", "x*exp(-x)"), ("xln(x)-x", "x*log(x)-x"),
                                       ("sinx", "sin(x)"), ("2xy", "2*x*y"), ("xsin(x)", "x*sin(x)"),
                                       ("e^x", "exp(x)"), ("tan(x)", "tan(x)")])
def test_glued_names(given, key):
    assert grading.check(q("expr", key, var="x,y"), given)["correct"] is True


def test_two_variables():
    assert grading.check(q("expr", "-x/y", var="x,y"), "-x/y")["correct"]
    assert not grading.check(q("expr", "-x/y", var="x,y"), "-y/x")["correct"]


def test_antiderivative_constant_irrelevant():
    k = q("antideriv", "x^3+2x^2")
    for g in ["x^3+2x^2", "x^3 + 2x^2 + C", "x^3+2x^2+7"]:
        assert grading.check(k, g)["correct"], g
    assert not grading.check(k, "3x^2+4x")["correct"]


@pytest.mark.parametrize("given,ok", [("pi/4", True), ("0.7854", True), ("0.79", False),
                                       ("π/4", True)])
def test_num(given, ok):
    assert grading.check(q("num", "pi/4"), given)["correct"] is ok


def test_mc_index_and_text():
    k = q("mc", "1", choices=["a", "b", "c"])
    assert grading.check(k, "1")["correct"]
    assert grading.check(k, "b")["correct"]
    assert not grading.check(k, "0")["correct"]


@pytest.mark.parametrize("evil", ["__import__('os')", "x; import os", "open('f')",
                                  "().__class__", "x" * 500, "lambda: 1"])
def test_rejects_unsafe_input(evil):
    res = grading.check(q("expr", "x"), evil)
    assert res["correct"] is False and res.get("unparseable")


def test_unexpected_variable_is_reported():
    res = grading.check(q("expr", "2x"), "2t")
    assert res.get("unparseable") and "unexpected" in res["feedback"]


@pytest.mark.parametrize("given,key,ok", [("3+4i", "3+4*I", True), ("4i+3", "3+4*I", True),
                                          ("(2-i)/5", "2/5-i/5", True), ("-1", "i^2", True),
                                          ("3-4i", "3+4*I", False), ("i", "sqrt(-1)", True)])
def test_complex_numbers(given, key, ok):
    assert grading.check(q("num", key), given)["correct"] is ok


def test_i_stays_a_variable_when_declared():
    assert grading.check(q("expr", "2*i", var="i"), "i+i")["correct"]
