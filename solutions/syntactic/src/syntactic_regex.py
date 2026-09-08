# source .jpamb-eval/bin/activate
# jpamb -vv analyse syntactic-regex

#!/usr/bin/env python3
"""A very stupid syntactic analysis, that only checks for assertion errors."""

import logging
import re
import sys
from pathlib import Path

import jpamb

log = logging

def check_inf_loop(body):
    inf_loop = re.search(r"while\s*\(\s*true\s*\)", body)
    if inf_loop:
        log.debug("Condition always true")
        return "found"
    
    empty_loop = re.search(r"while\s*\(([^)]+)\s*\)\s*\{\s*\}", body)

    if empty_loop:
        condition = empty_loop.group(1)
        mutates = re.search(r"\+\+|--|\+=|-=", condition)
        if not mutates:
            log.debug("Condition never modifies anything")
            return "found"

    log.debug("No confident signal for infinte loop")
    return "skip"


def check_null_pointer(body):
    """Return 'found' or 'skip' for null pointer exceptions."""

    null_assign = re.search(r"(\w+)(?:\[\])?\s*=\s*null\b", body)
    if null_assign:
        var = null_assign.group(1)

        reassigned = re.search(rf"\b{re.escape(var)}\s*=(?!=)\s*(?!null\b)", body)

        if not reassigned:
            dereferenced = re.search(rf"\b{re.escape(var)}(\.length\b|\[\w+\])", body)
            if dereferenced:
                log.debug("Unreassigned null variable %s is dereferenced", var)
                return "found"

    log.debug("No confident signal for null pointer")
    return "skip"

def check_divide_by_zero(sig, body):
    div_by_zero = re.search(r"/\s*0(?!\d)", body)
    if div_by_zero:
        log.debug("Division by zero found")
        return "found"

    param_denom = re.search(r"int\s+(\w+)", sig)
    if param_denom:
        param = param_denom.group(1)
        div_by_param = re.search(rf"/\s*{re.escape(param)}\b", body)

        if div_by_param:
            guard = re.search(rf"if\s*\(\s*{re.escape(param)}\s*!=\s*0", body[:div_by_param.start()])
            if not guard:
                log.debug("Unguarded division by parameter %s", param)
                return "found"

    log.debug("No confident signal for divide by zero")
    return "skip"

def find_method_body(rest):
    """Return the method body text from it opening { to the matching closing }."""
    start = rest.index("{")
    depth = 0
    for i in range(start, len(rest)):
        if rest[i] == "{":
            depth += 1
        elif rest[i] == "}":
            depth -= 1
            if depth == 0:
                return rest[start:i + 1]
    return rest

def check_assertion_error(body):
    if "assert" in body:
        log.debug("Found assertion")
        return "found"
    else:
        log.debug("No assertion")
        return "not-found"

def check_out_of_bounds(sig, body):
    """Return 'found', 'not-found', or 'skip' for OOB array access."""

    # 1: a parameter array indexed at a literal position, with no length guard
    array_param = re.search(r"\w+\[\]\s+(\w+)", sig)
    if array_param:
        array_name = array_param.group(1)
        index_access = re.search(rf"\b{re.escape(array_name)}\[\d+\]", body)

        if index_access:
            guard = re.search(rf"{re.escape(array_name)}\.length", body[:index_access.start()])
            if not guard:
                log.debug("Unguarded literal index into parameter array %s", array_name)
                return "found"

    # 2: array indexed inside a loop bounded by same array's length
    loop_match = re.search(
        r"for\s*\(\s*int\s+(\w+)\s*=\s*\d+;\s*\1\s*<\s*(\w+)\.length", body
    )
    if loop_match:
        loop_var = loop_match.group(1)
        arr_name = loop_match.group(2)
        body_after_loop = body[loop_match.end():]
        index_in_loop = re.search(rf"{re.escape(arr_name)}\[{re.escape(loop_var)}\]", body_after_loop)
        if index_in_loop:
            log.debug("Loop-bounded safe indexing of %s by %s", arr_name, loop_var)
            return "not-found"

    # 3: local array indexed
    array_init = re.search(r"int\s+(\w+)\[\]\s*=\s*\{([^}]+)\}", body)
    if array_init:
        array_name = array_init.group(1)
        arr_elements_text = array_init.group(2)
        elements = arr_elements_text.split(",")
        arr_size = len(elements)

        arr_index = re.search(rf"\b{array_name}\[(\d+)\]", body)
        if arr_index:
            index = int(arr_index.group(1))
            if index >= arr_size:
                log.debug("Literal index %d out of bounds for %s (size %d)", index, array_name, arr_size)
                return "found"
            else:
                log.debug("Literal index %d within bounds for %s (size %d)", index, array_name, arr_size)

    log.debug("No confident signal for out of bounds")
    return "skip"



def main():
    absmethodid = jpamb.getmethodid(
        "syntaxer",
        "1.0",
        "The Rice Theorem Cookers",
        ["syntactic", "python"],
        for_science=True,
    )

    log.basicConfig(level=logging.DEBUG)
    log.debug(Path.cwd())

    suite, _ = jpamb.setup()

    srcfile = suite.sourcefile(absmethodid.classname).relative_to(Path.cwd())

    with open(srcfile, "r") as f:
        log.debug("parse sourcefile %s", srcfile)
        content = f.read()

    res = re.search(rf".* {absmethodid.methodid.name}\(.*\)", content)

    if not res:
        log.error("Could not find method")
        sys.exit(1)

    log.debug(f"found {res}")
    rest = content[res.end(0) : -1]
    sig = res.group(0)
    body = find_method_body(rest)

    results = {
        "assertion error": check_assertion_error(body),
        "out of bounds": check_out_of_bounds(sig, body),
        "divide by zero": check_divide_by_zero(sig, body),
        "null pointer": check_null_pointer(body),
        "*": check_inf_loop(body),
    }

    divide_or_end = re.search(r"/|(^\s*})", rest, re.MULTILINE)

    if not divide_or_end:
        log.error("Could not find end of method or divide")
        log.error(rest)
        sys.exit(1)

    log.debug(f"found divide {divide_or_end}")
    divide_found = divide_or_end.group(0) == "/"

    if divide_found:
        log.debug("Found divide")
        print("divide by zero;found-div")
    else:
        log.debug("No divide")
        print("divide by zero;not-found-div")

    for q in jpamb.QUERIES:
        if q == "divide by zero":
            continue
        print(f"{q};{results.get(q, 'skip')}")

if __name__ == "__main__":
    main()
