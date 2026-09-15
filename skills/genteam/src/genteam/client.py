"""GenTeam REST access and shared channel naming, without private defaults."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Settings

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class APIError(RuntimeError):
    pass


class AuthExpired(APIError):
    pass


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Credentials belong to exactly the requested endpoint. No implicit
        # redirect is needed by these API calls, so do not forward any header.
        return None


def open_request(request, *, timeout=60):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def rows(response: dict, field: str) -> list[dict]:
    value = response.get(field)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise APIError(f"GenTeam returned no valid {field} list")
    return value


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = settings.base_url + "/api/digital-employee"

    def cookie(self) -> str:
        try:
            value = self.settings.cookie_file.read_text().strip()
        except OSError:
            raise AuthExpired("GenTeam cookie file is missing or unreadable") from None
        if not value or any(char in value for char in "\r\n;"):
            raise AuthExpired("GenTeam cookie file is empty or invalid")
        return value

    def request(self, method: str, path: str, body=None, params=None, *, cookie=None):
        value = self.cookie() if cookie is None else cookie
        if not value or any(character in value for character in "\r\n;"):
            raise AuthExpired("GenTeam cookie input is empty or invalid")
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
        headers = {
            "Cookie": f"{self.settings.cookie_name}={value}",
            "Accept": "application/json",
            "User-Agent": self.settings.get("user_agent", USER_AGENT),
        }
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with open_request(request, timeout=60) as response:
                    result = json.load(response)
                if not isinstance(result, dict):
                    raise APIError("GenTeam returned an invalid response")
                return result
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthExpired(f"GenTeam rejected the cookie (HTTP {exc.code})") from None
                if (exc.code == 429 or exc.code >= 500) and attempt + 1 < attempts:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise APIError(f"GenTeam {method} failed (HTTP {exc.code})") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt + 1 < attempts:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise APIError(f"GenTeam {method} transport did not confirm success") from None
            except (ValueError, UnicodeError):
                raise APIError("GenTeam returned invalid JSON") from None

    def channels(self, *, include_threads=True, include_pins=False):
        """Yield channel, members, server id and server slug."""
        for server in rows(self.request("GET", "/servers"), "servers"):
            if not server.get("id"):
                raise APIError("GenTeam returned a workspace without an id")
            if server.get("deleted_at") or server.get("archived_at"):
                continue
            slug = server.get("slug") or server["id"]
            resolved = self.request("GET", "/servers/resolve", params={"slug": slug})
            pinned_ids = set()
            if include_pins:
                viewer = resolved.get("viewer")
                pins = viewer.get("pinned_channel_ids") if isinstance(viewer, dict) else None
                if not isinstance(pins, list) or any(
                    not isinstance(cid, str) or not cid for cid in pins
                ):
                    raise APIError("GenTeam returned no valid viewer.pinned_channel_ids list")
                pinned_ids = set(pins)
            members = {member.get("actor_id"): member for member in rows(resolved, "members")}
            for channel in rows(resolved, "channels"):
                if not channel.get("id"):
                    raise APIError("GenTeam returned a channel without an id")
                if channel.get("archived_at") or (
                    not include_threads and channel.get("channel_type") == "thread"
                ):
                    continue
                if include_pins:
                    channel = {**channel, "pinned": channel["id"] in pinned_ids}
                yield channel, members, server["id"], slug


def slugify(name: str) -> str:
    value = re.sub(r"[^\w一-鿿-]+", "-", name.strip().lower())
    return re.sub(r"-{2,}", "-", value).strip("-") or "unnamed"


def channel_label(channel: dict, members: dict) -> str:
    if channel.get("channel_type") == "dm":
        names = []
        for participant in channel.get("dm_participants") or []:
            actor_id = participant.get("actor_id") if isinstance(participant, dict) else participant
            member = members.get(actor_id)
            if member and member.get("display_name"):
                names.append(member["display_name"])
        if names:
            return "dm-" + "-".join(slugify(name) for name in sorted(names))
    return channel.get("name") or channel.get("display_name") or channel["id"]
