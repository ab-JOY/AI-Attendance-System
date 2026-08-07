## Session Start — Read Before Touching Anything

When starting cold (new session, no prior context in this conversation), read
these **before** planning, exploring, or editing:

1. **`tasks/handover-*.md` — the most recent one.** Written at the end of each
   sprint for the agent picking up next. It carries the current verified
   state, what the last sprint changed and why, corrections to earlier
   findings, and the specific traps in this codebase. If several exist, read
   the latest; earlier ones are history, not instructions.
2. **`tasks/todo.md`** — the ISO/IEC 25010 audit and phased refactoring plan.
   Findings carry IDs (`SE-15`, `PE-0`, `FS-3`). **Cite the ID** when you work
   on one. Do not re-derive the audit; it is already done and measured.
3. **`tasks/lessons.md`** — mistakes already made here. Do not repeat them.

**Why this is mandatory, not advisory:** this project has failure modes that
look like working code. Two files execute on import. Importing
`recognize_face.py` costs ~9 seconds. A parameter that reads as an
optimisation silently broke recognition for every user. The handover names
these; discovering them yourself costs hours.

### Hard rules

- **Never commit `dataset/` or `trainer/`.** They hold face images of
  identifiable students and biometric templates derived from them — sensitive
  personal information under RA 10173. Git history is permanent and copies to
  every clone; deleting the files later does not undo it. Both are
  gitignored. Never `git add -f` them. Before any bulk `git add`, verify:
  `git check-ignore -q dataset trainer && echo SAFE || echo STOP`
- **Decisions marked as the user's are not yours.** `tasks/todo.md` §7 lists
  open decisions that shape the thesis. Bring measured evidence and options;
  do not choose unilaterally.
- **Accuracy numbers in this project are not what they appear.** Before
  quoting any figure, read `docs/walkthrough.md` §4 (limitations) and §5
  (correction log).

### At sprint end

Write `tasks/handover-<phase>.md` for the next agent: verified current state
with measured numbers, what changed and why, new findings, corrections to
earlier claims, per-task warnings for the next phase, and open decisions.
Assume the reader has no memory of this session.

---

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One tack per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fizing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
1. **Plan First**: Write plan to tasks/todo.md' with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. Document Results**: Add review section to tasks/todo.md'
6. **Capture Lessons**: Update tasks/lessons.md' after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.