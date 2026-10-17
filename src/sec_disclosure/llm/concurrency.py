"""Bounded job scheduling that drains admitted work before surfacing failures."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def run_jobs(jobs, work, consume, workers):
    if workers == 1:
        for job in jobs:
            consume(job, work(job))
        return
    remaining = iter(jobs)
    error = None
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {}

        def fill():
            while error is None and len(pending) < workers:
                try:
                    job = next(remaining)
                except StopIteration:
                    break
                pending[executor.submit(work, job)] = job

        fill()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                if future.cancelled():
                    continue
                try:
                    consume(job, future.result())
                except BaseException as failure:
                    if error is None:
                        error = failure
            if error is not None:
                for future in pending:
                    future.cancel()
            fill()
    if error is not None:
        raise error
