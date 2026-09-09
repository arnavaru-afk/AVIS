"""Container entry point for the AVIS scheduler."""

from apps.scheduler.runtime import run_worker

if __name__ == "__main__":
    run_worker()
