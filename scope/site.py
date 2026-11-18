"""本地环回测试站点。

serve() 保持原有单站行为；serve_pair() 提供两个独立来源，用于验证跨来源
重定向的凭据剥离与逐跳闸门。state 可在运行时调整 robots 内容/状态、
重定向表，以及通过 fail_paths 制造 5xx（验证重试不重新获得节奏额度）。
"""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ROBOTS = 'User-agent: PartnerCollector\nDisallow: /private/\nCrawl-delay: 1\n'
DEFAULT_PAGE = '<article><h1>活动公告</h1><p>合作方公开页面</p></article>'


def _make_state(robots=DEFAULT_ROBOTS):
    return {'robots': robots, 'robots_status': 200, 'redirects': {},
            'fail_paths': set(), 'requests': []}


def _make_server(state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append({
                'path': self.path,
                'origin': f'http://{self.headers.get("Host")}',
                'cookie': self.headers.get('Cookie'),
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
            elif self.path in state['fail_paths']:
                self.send_error(503)
                return
            elif self.path.startswith(
                    ('/public/', '/private/', '/inflight/', '/temp/')):
                data = DEFAULT_PAGE.encode()
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
    return server, thread


@contextmanager
def serve():
    state = _make_state()
    server, thread = _make_server(state)
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def serve_pair():
    """两个独立来源的测试站，返回 ((url_a, state_a), (url_b, state_b))。"""
    state_a = _make_state()
    state_b = _make_state()
    server_a, thread_a = _make_server(state_a)
    server_b, thread_b = _make_server(state_b)
    try:
        yield ((f'http://127.0.0.1:{server_a.server_port}', state_a),
               (f'http://127.0.0.1:{server_b.server_port}', state_b))
    finally:
        for server, thread in ((server_a, thread_a), (server_b, thread_b)):
            server.shutdown()
            server.server_close()
            thread.join()
