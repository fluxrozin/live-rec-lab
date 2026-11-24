"""Account input profiles used by the service adapters."""

import re
import unicodedata
from urllib.parse import unquote, urlsplit

_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_TIKTOK_ID = re.compile(r"[A-Za-z0-9_.]{2,24}\Z")
_TWITCASTING_ID = re.compile(r"[A-Za-z0-9_]{1,50}\Z")


def normalize_target(service: str, value: str) -> str:
    if service not in {"tiktok", "twitcasting"}:
        raise ValueError("unknown service")
    if len(value) > 2048 or "\\" in value or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("invalid account input")
    account = value.strip()
    if "://" in account:
        account = _from_profile(service, account)
    if service == "tiktok":
        account = account.removeprefix("@")
        if _TIKTOK_ID.fullmatch(account) and ".." not in account:
            return account
    elif _TWITCASTING_ID.fullmatch(account):
        return account
    raise ValueError("invalid account identifier")


def _from_profile(service: str, value: str) -> str:
    url = urlsplit(value)
    hosts = _TIKTOK_HOSTS if service == "tiktok" else {"twitcasting.tv"}
    if (
        url.scheme != "https"
        or url.hostname not in hosts
        or url.username is not None
        or url.password is not None
        or url.port not in (None, 443)
        or "?" in value
        or "#" in value
    ):
        raise ValueError("invalid profile URL")
    if re.search(r"%(?![0-9A-Fa-f]{2})", url.path):
        raise ValueError("invalid profile URL encoding")
    path = url.path.split("/")
    if service == "tiktok" and (len(path) == 2 or len(path) == 3 and path[2] == "live") and path[1].startswith("@"):
        return unquote(path[1], errors="strict")
    if service == "twitcasting" and (len(path) == 2 or len(path) == 3 and not path[2]):
        return unquote(path[1], errors="strict")
    raise ValueError("invalid profile path")
