import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def serve():
    state = {'robots': 'User-agent: PartnerCollector\nDisallow: /private/\nCrawl-delay: 1\n',
             'robots_status': 200, 'redirects': {}, 'requests': []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append({'path': self.path, 'cookie': self.headers.get('Cookie'),
                                      'authorization': self.headers.get('Authorization')})
            if self.path in state['redirects']:
                self.send_response(302)
                self.send_header('Location', state['redirects'][self.path])
                self.end_headers()
                return
            if self.path == '/robots.txt':
                data = state['robots'].encode()
                self.send_response(state['robots_status'])
                self.send_header('Content-Type', 'text/plain')
            elif self.path.startswith(('/public/', '/private/')):
                data = '<article><h1>活动公告</h1><p>合作方公开页面</p></article>'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            else:
                self.send_error(404)
                return
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
