"""Dashboard route contract used by the isolated installer tests."""
from urllib.parse import urlsplit


class DashboardHandler:
    def do_HEAD(self) -> None:
        request_path = urlsplit(self.path).path
        if not self.ensure_authorized():
            return
        self.serve_private(request_path)

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        if not self.ensure_authorized():
            return
        self.serve_private(request_path)
