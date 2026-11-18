import json
import tempfile
from pathlib import Path
from scope.intake import Intake
from scope.permission import Permit
from scope.requests import PageClient
from scope.site import serve


def main():
    with tempfile.TemporaryDirectory() as directory, serve() as (url, state):
        client = PageClient(Permit({url: ['/public/']}))
        intake = Intake(Path(directory) / 'pages.sqlite3')
        intake.record(client.fetch(url + '/public/event-1'))
        print(json.dumps({'pages': intake.rows(), 'requests': state['requests']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
