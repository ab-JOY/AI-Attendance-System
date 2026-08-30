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

training_job = BackgroundJob(name="train_model", runner=train_model)
