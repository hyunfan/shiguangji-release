"""Request origin checks for the loopback-only service."""
import secrets


def tokens_equal(supplied, expected):
    """Constant-time token check that rejects (instead of raising on) non-ASCII input."""
    try:
        supplied_bytes = str(supplied or "").encode("utf-8", "surrogateescape")
    except UnicodeError:
        return False
    return secrets.compare_digest(supplied_bytes, expected.encode("utf-8"))


def local_origins(port):
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def valid_host(host, port):
    return host in {f"127.0.0.1:{port}", f"localhost:{port}"}
