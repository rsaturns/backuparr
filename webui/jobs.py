"""Short-lived jobs a browser starts and then polls (Plex sign-in, Prowlarr
discovery). A job is visible only to the browser that started it, which an
owner cookie identifies - separate from Flask's refreshing session cookie, so
a stale concurrent response can't clobber the session."""
import secrets
import threading

from flask import request

from webui import cookies


class OwnedJobs:
    def __init__(self, cookie, expired):
        self.cookie = cookie
        self.expired = expired  # job -> whether it can be forgotten
        self.jobs = {}  # job id -> (owner, job)
        self.lock = threading.Lock()

    def owner(self):
        """(this browser's owner id, the id to set as its new cookie or None)."""
        current = request.cookies.get(cookies.name(self.cookie))
        if current:
            return current, None
        new = secrets.token_urlsafe(24)
        return new, new

    def purge_locked(self):
        for job_id, (_, job) in list(self.jobs.items()):
            if self.expired(job):
                del self.jobs[job_id]

    def add(self, owner, job):
        job_id = secrets.token_urlsafe(24)
        with self.lock:
            self.purge_locked()
            self.jobs[job_id] = (owner, job)
        return job_id

    def mine_locked(self, job_id):
        """The job, if this browser started it and it hasn't expired."""
        self.purge_locked()
        owner, job = self.jobs.get(job_id, (None, None))
        return job if owner is not None and owner == request.cookies.get(cookies.name(self.cookie)) else None

    def current_locked(self, job_id):
        return self.jobs.get(job_id, (None, None))[1]

    def discard(self, job_id):
        with self.lock:
            self.jobs.pop(job_id, None)

    def discard_owner(self, owner):
        with self.lock:
            for job_id, (job_owner, _) in list(self.jobs.items()):
                if job_owner == owner:
                    del self.jobs[job_id]

    def set_cookie(self, response, new_owner, secure):
        # Set only when new, so a browser's own cookie is never echoed back.
        if new_owner:
            response.set_cookie(cookies.name(self.cookie), new_owner, httponly=True, secure=secure, samesite="Strict")
