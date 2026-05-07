# -*- coding: utf-8 -*-
"""Shared HTTP utility for BlenderGIS operators."""
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TIMEOUT = 30
USER_AGENT = 'Mozilla/5.0 BlenderGIS'


def http_get(url: str, timeout: int = TIMEOUT) -> bytes:
	"""GET request returning raw bytes. Raises URLError/HTTPError on failure."""
	rq = Request(url, headers={'User-Agent': USER_AGENT})
	with urlopen(rq, timeout=timeout) as r:
		return r.read()


def format_http_error(e) -> str:
	"""Human-readable error message from URLError or HTTPError."""
	code = getattr(e, 'code', None)
	reason = getattr(e, 'reason', str(e))
	if code in (401, 403):
		return "Access denied ({}). Service requires authentication.".format(code)
	elif code == 404:
		return "Service not found (404). Check the URL."
	elif code:
		return "HTTP error {}: {}".format(code, reason)
	return "Connection error: {}".format(reason)
