"""SAQ worker settings smoke test: importable without Redis, and SAQ accepts the dict."""

from saq.worker import Worker, import_settings

from app.jobs.build_job import build_model
from app.jobs.classify_job import classify_and_propose
from app.jobs.queue import BUILD_JOB, CLASSIFY_JOB


def test_settings_dict_lists_both_jobs():
    settings = import_settings("app.jobs.worker_settings.settings")  # what `saq <path>` does
    assert settings["functions"] == [classify_and_propose, build_model]
    assert settings["concurrency"] == 4
    assert callable(settings["startup"]) and callable(settings["shutdown"])
    assert settings["shutdown_grace_period_s"] < 120  # below the ECS stopTimeout

    worker = Worker(**settings)  # no I/O until start()
    assert set(worker.functions) == {CLASSIFY_JOB, BUILD_JOB}
