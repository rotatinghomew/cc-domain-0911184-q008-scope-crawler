import hashlib
import sqlite3


class Intake:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS pages(
            url TEXT PRIMARY KEY, digest TEXT, body BLOB, status INTEGER)''')

    def record(self, response):
        if response['status'] != 200 or not isinstance(response['body'], bytes):
            raise ValueError('invalid fetched page')
        digest = hashlib.sha256(response['body']).hexdigest()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)',
                            (response['url'], digest, response['body'], response['status']))

    def rows(self):
        return [dict(row) for row in self.db.execute('SELECT url,digest,status FROM pages ORDER BY url')]
