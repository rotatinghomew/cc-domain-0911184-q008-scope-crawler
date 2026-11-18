from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


class PageClient:
    def __init__(self, permit):
        self.permit = permit
        self.opener = build_opener(NoRedirect())

    def fetch(self, url):
        if not self.permit.allowed(url):
            raise PermissionError('URL is outside the configured source paths')
        request = Request(url, headers={'User-Agent': 'PartnerCollector/1.0'})
        with self.opener.open(request, timeout=5) as response:
            if response.headers.get_content_type() != 'text/html':
                raise ValueError('expected HTML page')
            return {'url': url, 'status': response.status, 'body': response.read(),
                    'content_type': response.headers.get('Content-Type')}
