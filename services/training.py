"""
The one background training job (PE-5).

A module-level singleton because there is exactly one model on disk and
`BackgroundJob` refuses a second concurrent run. It lives here rather than in a
blueprint because three different places start it - finishing an enrolment,
the explicit Train Model button, and any future re-enrolment path - and a job
object owned by one of them would make the other two import a web module to
reach it.
"""

from __future__ import annotations

from infra.jobs import BackgroundJob
from train_model import train_model


def train_and_preload(report=None):
    """
    Retrain, then read the new model for attendance while still "running".

    Without the second step the next Start read trainer.yml itself, 8-21 s
    with the operator waiting (vision/session.py `preload_model`). Inside the
    job rather than after it, so no second retrain can start and rename the
    files while they are being read (lessons.md L36).
    """
    ok, message = train_model(report=report)

    if ok:
        if report is not None:
            report("Preparing the model for attendance")

        # Deferred: importing recognize_face costs seconds and loads
        # MediaPipe, which nothing else in this module needs.
        from recognize_face import session

        session.preload_model()

    return ok, message


training_job = BackgroundJob(name="train_model", runner=train_and_preload)
