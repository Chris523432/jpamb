from dataclasses import dataclass
from dynamic import *

@dataclass
class Trace:
    input: jpamb.case.Input
    pcs: list
    outcome: str

def select_trace(bc, methodid, input, max_steps) -> Trace:
    state = initial(bc, methodid, input)

    pcs = []
    for x in range(max_steps):
        pc, state = step(bc, state)
        pcs.append((pc.method, pc.offset))

        if isinstance(state, str):
            return Trace(input=input, pcs=pcs, outcome=state)

    return Trace(input=input, pcs=pcs, outcome="*")

def analyse_traces(traces: list, number_of_params: int) -> dict:
    outcomes_seen = set()
    for trace in traces:
        outcomes_seen.add(trace.outcome)

    saw_all_traces = False
    if number_of_params == 0:
        if "*" not in outcomes_seen:
            saw_all_traces = True

    answers = {}
    for query in jpamb.QUERIES:
        if query in outcomes_seen:
            if query == "*":
                answers[query] = "timeout"
            else:
                answers[query] = "found"
        else:
            if saw_all_traces:
                answers[query] = "no"
            else:
                answers[query] = "not-found"
    return answers