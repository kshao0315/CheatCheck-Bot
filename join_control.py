"""Persist the user's global stop across workers, new jobs and restarts."""


def automatic_join_enabled(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        return True
    row=db.execute("SELECT value FROM settings WHERE key='automatic_join_enabled'").fetchone()
    return row is None or str(row[0]).strip().lower() not in ('0','false','off')
