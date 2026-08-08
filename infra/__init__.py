"""
Process-level plumbing: the camera reader, and the database pool.

`todo.md` §4's target architecture puts logging, the database pool, the camera
and the atomic model store here, separate from `vision/` (which is decision
logic with no hardware in it) and from the web layer. Phase 3 starts the
package with the two pieces it needs; the rest arrives with Phase 5's split of
`app.py`.

Nothing here imports Flask, MediaPipe or the LBPH model, so it stays cheap and
testable with fakes.
"""
