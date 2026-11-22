import re
from fractions import Fraction

_ID = re.compile(r"[A-Za-z0-9_:-]{1,64}")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?")


def fields(obj, required, optional=(), where="object"):
    """Require exactly the listed keys (plus optional ones) and no duplicates.

    Duplicate JSON keys are already rejected by the parser; this catches
    structural mismatches with a message that names the offending location.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"{where} must be a JSON object")
    missing = set(required) - obj.keys()
    extra = obj.keys() - set(required) - set(optional)
    if missing:
        raise ValueError(f"{where} is missing field(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{where} has unexpected field(s): {', '.join(sorted(extra))}")
    return obj


def array(value, low, high, where="array"):
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise ValueError(f"{where} must be an array of size {low}..{high}")
    return value


def exact_array(value, size, where="array"):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{where} must be an array of size {size}")
    return value


def integer(value, low, high, where="value"):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{where} must be an integer {low}..{high}")
    return value


def ident(value, where="id"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{where} must be 1..64 characters of [A-Za-z0-9_:-]")
    return value


def unique(values, where="identity"):
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {where}")


def number(value, where="number"):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise ValueError(f"{where} must be a bounded decimal string")
    return Fraction(value)


# Derived coordinates are exact rationals.  With the contract's 9-digit /
# 6-decimal bound on inputs, a normal-equation determinant can produce
# denominators of several hundred digits, so the rendering bound is generous
# while still finite.
_RATIONAL = re.compile(r"-?(?:0|[1-9][0-9]{0,4000})(?:/[1-9][0-9]{0,4000})?")


def rational(value, where="value"):
    """Validate a canonical exact-rational rendering emitted by this tool."""
    if (
        not isinstance(value, str)
        or len(value) > 9000
        or not _RATIONAL.fullmatch(value)
    ):
        raise ValueError(f"{where} must be a canonical integer or n/d rational string")
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        if Fraction(int(numerator), int(denominator)) != Fraction(value):
            raise ValueError(f"{where} rational is not in lowest terms")
    return value


def vector(value, size, where="vector"):
    return tuple(
        number(x, f"{where}[{i}]")
        for i, x in enumerate(exact_array(value, size, where))
    )


def matrix(value, where="projection"):
    return tuple(vector(row, 4, f"{where}[{i}]") for i, row in enumerate(exact_array(value, 3, where)))
