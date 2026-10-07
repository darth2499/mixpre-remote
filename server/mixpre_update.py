"""
MixPre Remote - self-update from GitHub.

The "Publish app update" workflow puts the app (not the OS) in a release called
"app-latest" (mixpre-app.tar.gz + manifest.json). When the Pi has internet it
checks that release, downloads newer versions in the background and verifies
the checksum. Installing (from Settings > Software, or automatically before
first use if enabled) switches /opt/mixpre-remote/current to the new version
and restarts. If the new version doesn't come up healthy, mixpre_guard.py
switches back to the previous one.

Layout:  /opt/mixpre-remote/releases/<ct>-<commit>/   app files
         /opt/mixpre-remote/current  -> releases/...  (what runs)
         /opt/mixpre-remote/previous -> releases/...  (rollback target)
"""
import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

import aiohttp

BASE = Path(os.environ.get("MIXPRE_BASE", "/opt/mixpre-remote"))
API = os.environ.get("MIXPRE_GITHUB_API", "https://api.github.com")
TAG = "app-latest"
MARKER = "update-pending.json"
FAILED = "update-failed.json"
CHECK_EVERY = 6 * 3600


def app_dir():
    return Path(__file__).resolve().parent


def read_build():
    try:
        return json.loads((app_dir() / "BUILD.json").read_text())
    except (OSError, ValueError):
        return {"version": "dev", "commit": "dev", "ct": 0, "system": 1}


class Updater:
    def __init__(self, bridge):
        self.b = bridge
        self.installed = read_build()
        self.status = "idle"      # idle|unset|checking|uptodate|downloading|ready|needs_image|installing|error
        self.message = ""
        self.available = None
        self.ready_dir = None
        self.failed_before = False
        self.last_check = 0
        self.notice = ""
        self._lock = asyncio.Lock()
        failed = BASE / FAILED
        if failed.exists():
            try:
                f = json.loads(failed.read_text())
                self.notice = f"The update to {f.get('version', 'a new version')} didn't start properly, so the previous version was restored."
                if f.get("to"):
                    self.cfg["update_skip"] = f["to"]   # don't auto-install that one again
                    self.b.save_config()
            except ValueError:
                pass
            failed.unlink(missing_ok=True)

    # ------------------------------------------------------------ settings
    @property
    def cfg(self):
        return self.b.config

    @property
    def repo(self):
        return str(self.cfg.get("update_repo") or "").strip().strip("/")

    def system_level(self):
        try:
            return int((BASE / "SYSTEM_LEVEL").read_text().strip())
        except (OSError, ValueError):
            return int(self.installed.get("system", 1))

    def headers(self, accept="application/vnd.github+json"):
        h = {"Accept": accept, "User-Agent": "mixpre-remote", "X-GitHub-Api-Version": "2022-11-28"}
        token = str(self.cfg.get("update_token") or "").strip()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def set(self, status, message=""):
        self.status, self.message = status, message
        self.b.net_changed()

    # ------------------------------------------------------------ status for the page
    def status_json(self):
        a = self.available or {}
        prev = BASE / "previous"
        return {
            "installed": {k: self.installed.get(k) for k in ("version", "commit", "ct")},
            "status": self.status, "message": self.message, "notice": self.notice,
            "available": {k: a.get(k) for k in ("version", "commit", "ct")} if a else None,
            "can_rollback": prev.is_symlink() and prev.resolve().exists() and prev.resolve() != app_dir(),
            "repo": self.repo, "auto": bool(self.cfg.get("update_auto")),
            "token_set": bool(self.cfg.get("update_token")), "last_check": int(self.last_check),
        }

    # ------------------------------------------------------------ check + download
    async def check(self, manual=False):
        if self._lock.locked():
            return
        async with self._lock:
            await self._check(manual)
        if self.status == "ready" and self.cfg.get("update_auto") and self.b.cmd_count == 0 \
                and not self.failed_before \
                and time.monotonic() - self.b.started_at < 600:
            self.b.emit_log("Update: installing automatically (nothing used yet since start-up)")
            await self.install()

    async def _check(self, manual):
        if not self.repo:
            return self.set("unset", "Set your GitHub repo below to get updates.")
        self.last_check = time.time()
        self.set("checking", "Checking for updates…")
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{API}/repos/{self.repo}/releases/tags/{TAG}", headers=self.headers()) as r:
                    if r.status == 404:
                        return self.set("error", "No update published yet (or the repo is private: add a GitHub token).")
                    if r.status in (401, 403):
                        return self.set("error", "GitHub refused access — check the token.")
                    r.raise_for_status()
                    rel = await r.json()
                assets = {a["name"]: a for a in rel.get("assets", [])}
                if "manifest.json" not in assets or "mixpre-app.tar.gz" not in assets:
                    return self.set("error", "The update release is incomplete — re-run “Publish app update”.")
                man = json.loads(await self._download(s, assets["manifest.json"]))
                self.available = man
                if int(man.get("ct", 0)) <= int(self.installed.get("ct", 0)):
                    return self.set("uptodate", "You're up to date.")
                if int(man.get("system", 1)) > self.system_level():
                    return self.set("needs_image", f"Version {man.get('version')} needs a new SD card image "
                                                   "(system software changed). Rebuild and reflash.")
                rel_dir = BASE / "releases" / f"{man['ct']}-{man.get('commit', 'x')}"
                self.failed_before = rel_dir.name == self.cfg.get("update_skip")
                if not (rel_dir / "BUILD.json").exists():
                    self.set("downloading", f"Downloading {man.get('version')}…")
                    data = await self._download(s, assets["mixpre-app.tar.gz"])
                    if hashlib.sha256(data).hexdigest() != man.get("sha256"):
                        return self.set("error", "Download didn't match its checksum — not installed. Try again.")
                    self._unpack(data, rel_dir)
                self.ready_dir = rel_dir
                self.b.emit_log(f"Update: {man.get('version')} ({man.get('commit')}) downloaded and verified")
                self.set("ready", f"Version {man.get('version')} is ready to install." +
                         (" (It failed to start last time.)" if self.failed_before else ""))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self.set("error", "Can't reach GitHub (no internet?)." if not manual else f"Couldn't check: {e}")
        except (ValueError, KeyError) as e:
            self.set("error", f"Bad update data: {e}")

    async def _download(self, session, asset):
        # API asset URL works for public and private repos (with a token)
        async with session.get(asset["url"], headers=self.headers("application/octet-stream")) as r:
            r.raise_for_status()
            return await r.read()

    def _unpack(self, data, dest):
        tmp = dest.with_name(dest.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        import io
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for m in tf.getmembers():
                p = Path(m.name)
                if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                    raise ValueError(f"unsafe path in bundle: {m.name}")
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:
                tf.extractall(tmp)
        if not (tmp / "mixpre_remote.py").exists() or not (tmp / "BUILD.json").exists():
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError("bundle is missing files")
        for f in ("mixpre_remote.py", "mixpre_net.py", "mixpre_cloud.py", "mixpre_update.py", "gadget.sh"):
            if (tmp / f).exists():
                os.chmod(tmp / f, 0o755)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)

    # ------------------------------------------------------------ install / rollback
    def _point(self, link_name, target):
        link, tmp = BASE / link_name, BASE / (link_name + ".tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, link)   # atomic switch

    def recording(self):
        return bool(self.b.transport & 16)

    async def install(self):
        if self.status != "ready" or not self.ready_dir:
            return
        if self.recording():
            return self.set("ready", "Stop recording first — updates never install while the MixPre is recording.")
        cur = app_dir()
        self.set("installing", "Installing — the controls will reconnect in about 15 seconds…")
        self.b.emit_log(f"Update: installing {self.available.get('version')}")
        self._point("previous", str(cur))
        self._point("current", str(self.ready_dir))
        (BASE / MARKER).write_text(json.dumps({
            "to": self.ready_dir.name, "version": self.available.get("version"),
            "from": cur.name, "installed_at": time.time(), "attempts": 0}))
        os.sync()
        await self._restart()

    async def rollback(self):
        prev = BASE / "previous"
        if not (prev.is_symlink() and prev.resolve().exists()):
            return self.set(self.status, "There's no previous version to go back to.")
        if self.recording():
            return self.set(self.status, "Stop recording first.")
        target, cur = prev.resolve(), app_dir()
        self.set("installing", "Going back to the previous version…")
        self.b.emit_log(f"Update: rolling back to {target.name}")
        self._point("current", str(target))
        self._point("previous", str(cur))
        (BASE / MARKER).unlink(missing_ok=True)
        os.sync()
        await self._restart()

    async def _restart(self):
        await asyncio.sleep(1.5)   # let the page get the message
        if self.b.fake:
            self.b.emit_log("Update: (fake mode) would restart now")
            self.installed = read_build()
            return
        proc = await asyncio.create_subprocess_exec("systemctl", "--no-block", "restart", "mixpre-remote.service")
        await proc.wait()

    # ------------------------------------------------------------ startup / background
    async def confirm_healthy(self):
        """Called once the new version is fully up: keep it."""
        marker = BASE / MARKER
        if not marker.exists():
            return
        await asyncio.sleep(20)
        try:
            m = json.loads(marker.read_text())
        except ValueError:
            m = {}
        if m.get("to") == app_dir().name:
            marker.unlink(missing_ok=True)
            self.notice = f"Updated to {self.installed.get('version')}."
            self.b.emit_log(f"Update: {self.installed.get('version')} is running fine")
            self.b.net_changed()
        self.cleanup()

    def cleanup(self):
        keep = {app_dir().name}
        for name in ("previous", "current"):
            p = BASE / name
            if p.is_symlink():
                keep.add(p.resolve().name)
        if self.ready_dir:
            keep.add(self.ready_dir.name)
        rel = BASE / "releases"
        for d in rel.iterdir() if rel.exists() else []:
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)

    async def loop(self):
        asyncio.ensure_future(self.confirm_healthy())
        while True:
            # wait until the Pi has internet (cloud connected or Wi-Fi says so)
            for _ in range(90):
                net = self.b.net
                if (self.b.cloud and self.b.cloud.connected) or (net and net.connectivity == "full"):
                    break
                await asyncio.sleep(10)
            await self.check()
            await asyncio.sleep(CHECK_EVERY)
