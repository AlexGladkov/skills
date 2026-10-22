#!/usr/bin/env python3
"""Restricted movie operations through Radarr; JSON output, no credential output."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request


class Failure(Exception):
    pass


def protected_json(path):
    path = Path(path)
    if path.is_symlink():
        raise Failure("Configuration must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise Failure("Configuration must be a root-owned file, not writable by group or others")
    parent = path.parent
    while True:
        if parent.is_symlink():
            raise Failure("Configuration directory must not be a symlink")
        directory = parent.stat()
        if directory.st_uid != 0 or directory.st_mode & 0o022:
            raise Failure("Configuration directories must be root-owned and not writable by others")
        if parent == parent.parent:
            break
        parent = parent.parent
    return json.loads(path.read_text())


class Radarr:
    def __init__(self, config, key):
        self.config = config
        self.key = key
        self.base = config["radarr_url"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Failure("Invalid configured Radarr URL")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, data=None):
        request = urllib.request.Request(
            self.base + "/api/v3/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"X-Api-Key": self.key, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise Failure(f"Radarr HTTP {error.code}; no automatic retry of writes") from None
        except (urllib.error.URLError, TimeoutError):
            raise Failure("Radarr unavailable or timed out; check status before repeating a write") from None

    def movie(self, tmdb):
        found = next((m for m in self.call("movie") if m.get("tmdbId") == tmdb), None)
        if found is None:
            raise Failure("Movie not registered; use add first")
        return found

    def releases(self, movie):
        return self.call("release?" + urllib.parse.urlencode({"movieId": movie["id"]}))

    def eligible(self, release):
        quality = release.get("quality", {}).get("quality", {})
        return (release.get("approved") is True
                and not release.get("rejections")
                and quality.get("resolution") == self.config["resolution"]
                and quality.get("source") in self.config["allowed_sources"]
                and 0 < release.get("size", 0) <= self.config["max_size_bytes"]
                and re.search(self.config["audio_title_pattern"], release.get("title", ""), re.I) is not None)


def release_id(release):
    value = str(release["indexerId"]) + "\0" + release["guid"]
    return hashlib.sha256(value.encode()).hexdigest()


def summary(movie):
    return {k: movie.get(k) for k in ("tmdbId", "title", "year", "hasFile")}


def execute(client, args):
    cfg = client.config
    if args.command == "find":
        return [summary(m) for m in client.call("movie/lookup?" + urllib.parse.urlencode({"term": args.query}))]
    if args.command == "status":
        movie = client.movie(args.tmdb)
        result = summary(movie)
        result["downloads"] = [{k: row.get(k) for k in ("title", "status", "size", "sizeleft", "estimatedCompletionTime", "trackedDownloadStatus", "statusMessages")}
                               for row in client.call("queue?pageSize=1000").get("records", []) if row.get("movieId") == movie["id"]]
        return result
    if args.command == "add":
        existing = next((m for m in client.call("movie") if m.get("tmdbId") == args.tmdb), None)
        if existing:
            return {"result": "already_registered", "movie": summary(existing)}
        roots = client.call("rootfolder")
        root = next((r for r in roots if r["path"] == cfg["root_folder"]), None)
        if not root or root.get("freeSpace", 0) < cfg["min_free_bytes"]:
            raise Failure("Configured movie directory unavailable or low free space")
        found = client.call("movie/lookup?" + urllib.parse.urlencode({"term": f"tmdb:{args.tmdb}"}))
        movie = next((m for m in found if m.get("tmdbId") == args.tmdb), None)
        if not movie:
            raise Failure("Movie not found")
        movie.update(qualityProfileId=cfg["quality_profile_id"], rootFolderPath=cfg["root_folder"],
                     monitored=False, minimumAvailability="released", addOptions={"searchForMovie": False})
        return {"result": "registered", "movie": summary(client.call("movie", movie))}
    movie = client.movie(args.tmdb)
    if movie.get("qualityProfileId") != cfg["quality_profile_id"] or Path(movie.get("path", "")).parent != Path(cfg["root_folder"]):
        raise Failure("Movie does not match the configured directory and profile")
    releases = client.releases(movie)
    if args.command == "releases":
        return [{"release_id": release_id(r), "title": r["title"], "size": r["size"],
                 "quality": r["quality"]["quality"]["name"], "seeders": r.get("seeders"),
                 "audio": "title matches configured pattern; actual tracks not verified"}
                for r in releases if client.eligible(r)]
    selected = next((r for r in releases if release_id(r) == args.release_id), None)
    if not selected or not client.eligible(selected):
        raise Failure("Release unavailable or rejected by policy")
    if movie.get("hasFile"):
        raise Failure("Movie already has a file; replacements are not allowed")
    queue = client.call("queue?pageSize=1000")
    if any(row.get("movieId") == movie["id"] for row in queue.get("records", [])):
        raise Failure("Movie already queued")
    if queue.get("totalRecords", 0) >= cfg["max_queue_items"]:
        raise Failure("Queue limit reached")
    root = next((r for r in client.call("rootfolder") if r["path"] == cfg["root_folder"]), None)
    if not root or root.get("freeSpace", 0) < selected["size"] + cfg["min_free_bytes"]:
        raise Failure("Insufficient free space")
    if not args.execute:
        return {"result": "preview", "movie": summary(movie), "title": selected["title"], "size": selected["size"]}
    client.call("release", {"guid": selected["guid"], "indexerId": selected["indexerId"], "movieId": movie["id"]})
    return {"result": "submitted", "movie": summary(movie), "note": "Check status; submission is not download completion"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("find").add_argument("query")
    for name in ("add", "releases", "download", "status"):
        command = commands.add_parser(name)
        command.add_argument("tmdb", type=int)
        if name == "download":
            command.add_argument("release_id")
            command.add_argument("--execute", action="store_true", help="Submit; default is preview")
    args = parser.parse_args()
    try:
        directory = Path("/Library/Application Support/nasctl" if sys.platform == "darwin" else "/etc/nasctl")
        config = protected_json(directory / "config.json")
        secrets = protected_json(directory / "secrets.json")
        if config["run_uid"] == 0 or os.getuid() != config["run_uid"]:
            raise Failure("Run only as the configured dedicated non-root user")
        output = execute(Radarr(config, secrets["radarr_api_key"]), args)
        print(json.dumps(output, ensure_ascii=False))
    except Failure as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "Invalid or inaccessible configuration or unexpected API response"}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
