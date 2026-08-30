"""
Orchestration that is not about HTTP (MA-1).

What belongs here is anything a route was doing that has nothing to do with
requests, responses or templates: wiring a capture session to MediaPipe and a
staging folder, owning the background training job, opening and closing an
attendance session.

The test for whether something belongs in `services/` rather than `web/`:
**could it be driven from a script with no Flask?** If yes, it goes here, and
the route becomes the four or five lines that read the form and choose a status
code. That is the difference between a route that is hard to test and a route
that hardly needs testing.
"""
