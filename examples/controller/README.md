# Controller and enum FSM examples

`controller.sv` uses a parameterized child, blocking combinational next-state logic, ordinary
case decode, and synchronous reset. `request.json` verifies one-cycle load behavior.
`fsm.sv` uses a packed enum and combinational next-state/output logic. Its request checks
idle and working outputs while requiring that their guards can actually be reached.

Run `opencollate sequential check examples/controller/request.json`. For independently checkable
evidence, use `sequential certify` and `sequential verify-certificate` with the same request.
The `formal` and `certificates` extras supply the optional producers; the certificate receiver
needs neither SAT nor SMT solver. Consult `docs/controller-verification.md` for precise boundaries.
