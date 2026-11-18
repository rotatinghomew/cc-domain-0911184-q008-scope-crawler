from urllib.parse import urlparse


class Permit:
    def __init__(self, origins):
        self.origins = {origin.rstrip('/'): tuple(prefixes) for origin, prefixes in origins.items()}

    def allowed(self, url):
        parsed = urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return False
        return any(parsed.path.startswith(prefix) for prefix in self.origins.get(origin, ()))
