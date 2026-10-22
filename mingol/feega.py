"""The Feega sign-in service's account API, over the network.

Players sign in to the game with a Feega login. The console sends that login's CommonInqID (10
digits that never change for the account) as its account key, so every account here (Store.by_key)
belongs to one Feega login. Feega runs as its own service; on the shared `openlobby` Docker network
it answers at http://feega:24030:

  GET /v1/accounts/<key>          {"key", "account"} or 404
  GET /v1/accounts?name=<login>   {"key", "account"} or 404

MINGOL_FEEGA_URL changes the address (empty turns the lookups off) and MINGOL_FEEGA_TOKEN is sent
as X-Feega-Token when Feega asks for one. Calls block: never make them on the game's event loop.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://feega:24030"
TIMEOUT = 3.0
KEY_RE = re.compile(r"^\d{10}$")


class FeegaError(Exception):
    """Feega did not answer, or answered something that is not an account."""


class Feega:
    def __init__(self, url=None, token=None, timeout=TIMEOUT):
        self.url = (os.environ.get("MINGOL_FEEGA_URL", DEFAULT_URL) if url is None else url).rstrip("/")
        self.token = os.environ.get("MINGOL_FEEGA_TOKEN", "") if token is None else token
        self.timeout = timeout

    def _get(self, path):
        """The JSON answer, or None for 404. Raises FeegaError for anything else."""
        if not self.url:
            raise FeegaError("no Feega address set")
        headers = {"X-Feega-Token": self.token} if self.token else {}
        req = urllib.request.Request(self.url + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise FeegaError("HTTP %d" % e.code) from None
        except (OSError, ValueError) as e:
            raise FeegaError(e.__class__.__name__) from None

    def account(self, key):
        """The login's name as it is spelled now, or None when Feega has no such account."""
        if not KEY_RE.match(str(key)):
            return None
        doc = self._get("/v1/accounts/" + str(key))
        return str(doc.get("account") or "") or None if isinstance(doc, dict) else None

    def key_of(self, name):
        """The key of the account a Feega login name signs in to, or None."""
        if not name:
            return None
        doc = self._get("/v1/accounts?" + urllib.parse.urlencode({"name": name}))
        key = str(doc.get("key") or "") if isinstance(doc, dict) else ""
        return key if KEY_RE.match(key) else None
