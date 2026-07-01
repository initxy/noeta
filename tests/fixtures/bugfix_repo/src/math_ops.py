"""A tiny module with a one-line bug a coding-Agent can spot + fix."""


def add(a: int, b: int) -> int:
    # BUG: subtracts instead of adds. Fixing the operator to `+`
    # makes tests/test_add.py pass.
    return a - b


def double(x: int) -> int:
    return x + x
