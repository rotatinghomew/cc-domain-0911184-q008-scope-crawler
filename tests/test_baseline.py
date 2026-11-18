import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from scope.intake import Intake
from scope.permission import Permit
from scope.requests import PageClient
from scope.site import serve


class ScopeTests(unittest.TestCase):
    def test_permitted_page_is_stored_with_digest(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, state):
            filename = Path(directory) / 'intake.db'
            intake = Intake(filename)
            intake.record(PageClient(Permit({url: ['/public/']})).fetch(url + '/public/1'))
            self.assertEqual(len(Intake(filename).rows()), 1)
            self.assertEqual(len(intake.rows()[0]['digest']), 64)

    def test_outside_path_never_sent(self):
        with serve() as (url, state):
            client = PageClient(Permit({url: ['/public/']}))
            with self.assertRaises(PermissionError):
                client.fetch(url + '/private/1')
            self.assertEqual(state['requests'], [])

    def test_existing_client_does_not_automatically_follow_redirect(self):
        with serve() as (url, state):
            state['redirects']['/public/old'] = '/private/new'
            with self.assertRaises(HTTPError) as error:
                PageClient(Permit({url: ['/public/']})).fetch(url + '/public/old')
            self.assertEqual(error.exception.code, 302)
            self.assertEqual([item['path'] for item in state['requests']], ['/public/old'])

    def test_robots_availability_is_configurable(self):
        with serve() as (url, state):
            with urlopen(url + '/robots.txt') as response:
                self.assertIn(b'Disallow: /private/', response.read())
            state['robots_status'] = 503
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '/robots.txt')
            self.assertEqual(error.exception.code, 503)

    def test_url_credentials_and_schemes_are_rejected(self):
        permit = Permit({'http://local': ['/']})
        for url in ['file:///tmp/local', 'http://name:password@local/a', 'ftp://local/a']:
            self.assertFalse(permit.allowed(url))

    def test_intake_does_not_accept_failed_response(self):
        intake = Intake(':memory:')
        with self.assertRaises(ValueError):
            intake.record({'url': 'http://local/1', 'status': 503, 'body': b'busy'})
        self.assertEqual(intake.rows(), [])
