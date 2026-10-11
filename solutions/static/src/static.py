import sys
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from abstractions import SignSet

import jpamb
import sexpr
from jpamb import jvm
from jvm.state import PC, StackInt


@dataclass(frozen=True)
class State(sexpr.AsSExpr):
    locals: tuple[SignSet, ...]
    stack: tuple[SignSet, ...]
    origins: tuple = ()

    def __post_init__(self):
        assert isinstance(self.locals, tuple)
        assert isinstance(self.stack, tuple)
        if len(self.origins) != len(self.stack):
            object.__setattr__(self, "origins", (None,) * len(self.stack))

    def __str__(self):
        return f"{', '.join(map(str, self.locals))}/{':'.join(map(str, self.stack))}"

    def __or__(self, other):
        assert isinstance(other, State), f"Expected State but got {other!r}"
        assert len(self.stack) == len(other.stack), "Stacks should be equal lenght"
        assert len(self.locals) == len(other.locals), "Locals should be equal lenght"

        origins = []
        for o1, o2 in zip(self.origins, other.origins):
            if o1 == o2:
                origins.append(o1)
            else:
                origins.append(None)
        return State(
            tuple(s1 | s2 for s1, s2 in zip(self.locals, other.locals)),
            tuple(s1 | s2 for s1, s2 in zip(self.stack, other.stack)),
            tuple(origins),
        )

    def push(self, value: SignSet, origin=None):
        assert isinstance(value, SignSet), f"Expected sign set but got {value}"
        return State(self.locals, self.stack + (value,), self.origins + (origin,))

    def pop(self, number=1):
        return self.stack[-number:], State(
            self.locals, self.stack[:-number], self.origins[:-number]
        )

    def origins_of(self, number):
        return self.origins[-number:]

    def load(self, index):
        return self.locals[index]

    def store(self, index, value):
        origins = []
        for o in self.origins:
            if o == index:
                origins.append(None)
            else:
                origins.append(o)
        return State(
            tuple(self.locals[:index]) + (value,) + tuple(self.locals[index + 1 :]),
            self.stack,
            tuple(origins),
        )
        
    def refine(self, origin, value: SignSet):
        if origin is None:
            return self
        new_local = self.locals[origin] & value
        return State(
            tuple(self.locals[:origin]) + (new_local,) + tuple(self.locals[origin + 1 :]),
            self.stack,
            self.origins,
        )


def manystep(
    bc: jpamb.Bytecode,
    pc: PC,
    state: State,
) -> Iterable[tuple[PC, object] | str]:
    opr = bc[pc]
    match opr:
        case jvm.Get(static=True, field=field):
            # Hack - Only handle the assertion case
            assert field.extension.name == "$assertionsDisabled"

            # Hack - Assuming assertions are never disabled
            va = SignSet.abstract([StackInt(0)])

            yield (pc + 1, state.push(va))

        case jvm.Ifz(condition=op, target=target):
            [o1] = state.origins_of(1)
            [val], after = state.pop(1)
            zero = SignSet.abstract([StackInt(0)])

            for outcome in [True, False]:
                kept = set()
                for x in val.signs:
                    if outcome in SignSet.compare(SignSet(frozenset([x])), zero, op):
                        kept.add(x)
                if len(kept) == 0:
                    continue 
                refined = after.refine(o1, SignSet(frozenset(kept)))
                if outcome:
                    yield (pc % target, refined)
                else:
                    yield (pc + 1, refined)

        case jvm.If(condition=op, target=target):
            [o1, o2] = state.origins_of(2)
            [v1, v2], after = state.pop(2)

            for outcome in [True, False]:
                kept1 = set()
                kept2 = set()
                for x in v1.signs:
                    for y in v2.signs:
                        one_x = SignSet(frozenset([x]))
                        one_y = SignSet(frozenset([y]))
                        if outcome in SignSet.compare(one_x, one_y, op):
                            kept1.add(x)
                            kept2.add(y)
                if len(kept1) == 0:
                    continue  
                refined = after.refine(o1, SignSet(frozenset(kept1)))
                refined = refined.refine(o2, SignSet(frozenset(kept2)))
                if outcome:
                    yield (pc % target, refined)
                else:
                    yield (pc + 1, refined)

        case jvm.Push(value=None):
            yield (pc + 1, state.push(SignSet.from_sign("0")))

        case jvm.Push(value=str()):
            # A string constant is a non-null reference
            yield (pc + 1, state.push(SignSet.from_sign("+")))

        case jvm.Push(value=v):
            yield (pc + 1, state.push(SignSet.abstract([StackInt(v)])))

        case jvm.Load(index=i):
            va = state.load(i)
            yield (pc + 1, state.push(va, origin=i))

        case jvm.Store(index=i):
            [va], after = state.pop(1)
            yield (pc + 1, after.store(i, va))

        case jvm.Incr(index=i, amount=amount):
            amount = SignSet.abstract([StackInt(amount)])
            res, _ = SignSet.arithmetic(state.load(i), amount, jvm.BinaryOpr.Add)
            yield (pc + 1, state.store(i, res))

        case jvm.Cast():
            # Truncating can change the sign, only 0 is guaranteed to stay 0
            [va], after = state.pop(1)
            if va.signs - {0}:
                va = SignSet.top()
            yield (pc + 1, after.push(va))

        case jvm.Dup():
            [o1] = state.origins_of(1)
            [va], _ = state.pop(1)
            yield (pc + 1, state.push(va, origin=o1))

        case jvm.NewArray():
            [_size], after = state.pop(1)
            yield (pc + 1, after.push(SignSet.from_sign("+")))

        case jvm.ArrayLength():
            [ref], after = state.pop(1)
            if 0 in ref.signs:
                yield "null pointer"
            if ref.signs - {0}:
                yield (pc + 1, after.push(SignSet.from_sign("0+")))

        case jvm.ArrayLoad():
            [ref, _index], after = state.pop(2)
            if 0 in ref.signs:
                yield "null pointer"
            if ref.signs - {0}:
                yield "out of bounds"
                # Can be anything
                yield (pc + 1, after.push(SignSet.top()))

        case jvm.ArrayStore():
            [ref, _index, _value], after = state.pop(3)
            if 0 in ref.signs:
                yield "null pointer"
            if ref.signs - {0}:
                # It might be out of bounds, no knowledge of length
                yield "out of bounds"
                yield (pc + 1, after)

        case jvm.InvokeStatic(method=m):
            n = len(m.extension.params)
            args, after = state.pop(n) if n > 0 else ((), state)

            # Analyse the callee, starting with the arguments as locals
            max_locals = bc.getmethod(m).max_locals
            locals = tuple(args) + (SignSet.bot(),) * (max_locals - n)
            yield (PC(m, 0), State(locals, ()))

            # We don't connect the return back, so the result can be anything
            if m.extension.return_type is not None:
                after = after.push(SignSet.top())
            yield (pc + 1, after)

        case jvm.InvokeVirtual(method=m):
            [ref, *_args], after = state.pop(len(m.extension.params) + 1)
            if 0 in ref.signs:
                yield "null pointer"
            if ref.signs - {0}:
                # We dont analyse library methods, so result is unknown
                if m.extension.return_type is not None:
                    after = after.push(SignSet.top())
                yield (pc + 1, after)

        case jvm.Goto(target=t):
            if t <= pc.offset:
                # Jumping backwards is a loop, which might never terminate
                yield "*"
            yield (pc % t, state)

        case jvm.Binary(operant=op):
            [v1, v2], after = state.pop(2)
            res, errors = SignSet.arithmetic(v1, v2, op)
            yield from errors
            if res != SignSet.bot():
                yield (pc + 1, after.push(res))

        case jvm.Return(type=None):
            yield "ok"

        case jvm.Return(type=t):
            # Hack -- we assume that we always return.
            yield "ok"

        case jvm.New(classname=jvm.ClassName("java.lang.AssertionError")):
            # Hack -- if we create an assertion error, we probably also throw it.
            yield "assertion error"


def initialstate(
    bc: jpamb.Bytecode,
    methodid: jvm.AbsMethodID,
    inputs: jpamb.Input | None,
) -> dict[PC, State]:
    method = bc.getmethod(methodid)
    locals = [SignSet.bot()] * method.max_locals

    if inputs is None:
        for i, p in enumerate(methodid.extension.params):
            locals[i] = SignSet.top()
    else:
        for i, x in enumerate(inputs.values):
            match x:
                case jpamb.case.Boolean(value=value):
                    locals[i] = SignSet.abstract([StackInt(int(value))])
                case jpamb.case.Int(value=value):
                    locals[i] = SignSet.abstract([StackInt(int(value))])
                case jpamb.case.Array() | jpamb.case.String():
                    locals[i] = SignSet.from_sign("+")
                case _:
                    raise NotImplementedError(f"Unsupported value {x!r}")

    state = State(tuple(locals), ())
    return {PC(methodid, 0): state}


@dataclass
class AbstractInterpreter:
    bc: jpamb.Bytecode
    worklist: deque[PC]
    states: dict[PC, State]

    @staticmethod
    def initial(bc: jpamb.Bytecode, methodid: jvm.AbsMethodID, inputs):
        states = initialstate(bc, methodid, inputs)
        worklist = deque(states.keys())

        return AbstractInterpreter(bc, worklist, states)

    def step(self) -> tuple[PC, set[str]]:
        pc = self.worklist.pop()

        print(f"Stepping {pc}:\n > {self.bc[pc]}", file=sys.stderr)

        finals = set()

        for res in manystep(self.bc, pc, self.states[pc]):
            if isinstance(res, str):
                finals.add(res)
            else:
                pc_, st = res

                before = self.states.get(pc_, None)
                if before is None:
                    after = st
                else:
                    after = before | st
                if before is None or after != before:
                    self.states[pc_] = after
                    self.worklist.append(pc_)

        return pc, finals


def interpret():
    """The static analysis"""
    methodid, input, steps = jpamb.getcase(
        "static",
        "1.0",
        "MeatballMice",
        ["static", "python"],
        for_science=True,
    )
    suite, eff = jpamb.setup()
    bc = jpamb.Bytecode(suite, eff, {})

    ai = AbstractInterpreter.initial(bc, methodid, input)

    x = jpamb.emit_init(ai.states)
    seen = set()

    while steps > 0 and ai.worklist:
        pc, final = ai.step()
        # Only report each final result once, to save steps
        for f in final - seen:
            jpamb.emit_step(x, pc, f, depth=1)
            steps -= 1
        seen |= final

        # Only report each program point once, to save steps
        if pc in seen:
            continue
        seen.add(pc)

        x = jpamb.emit_step(x, pc, ai.states, depth=1)
        steps -= 1


def analyse():
    """The static analysis"""

    methodid = jpamb.getmethodid(
        "static",
        "1.0",
        "MeatballMice",
        ["static", "python"],
        for_science=True,
    )

    suite, eff = jpamb.setup()
    bc = jpamb.Bytecode(suite, eff, {})

    steps = 300

    ai = AbstractInterpreter.initial(bc, methodid, None)

    final = set()
    while steps > 0 and ai.worklist:
        _pc, finals = ai.step()
        final |= finals
        steps -= 1

    for f in jpamb.QUERIES:
        if f not in final:
            print(f"{f};no")
        else:
            print(f"{f};maybe")

@dataclass(frozen=True)
class State:
    locals: tuple[SignSet, ...]
    stack: tuple[SignSet, ...]
    origins: tuple[int | None, ...]
    lens_locals: tuple[Interval, ...]   # New (parallel to locals)
    lens_stack: tuple[Interval, ...]    # New (parallel to stack)

    def __or__(self, other):
        # TODO: join each field