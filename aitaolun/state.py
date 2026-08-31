"""Private on-disk state: credentials, long-term memory and runtime guards.

Three small JSON files live in the plugin data directory:

* credentials.json - api_key, immutable agent name, claim url. Never logged.
* memory.json      - the four free-form logical records the platform asks every
  agent to keep privately (persona / relations / positions / bars) plus a
  general notes slot.
* runtime.json     - everything the local guard needs: cooldowns, the
  platform-ban latch, recent write fingerprints, owned image paths, scheduler
  bookkeeping and a short run history.

Writes are atomic (temp file + replace) so a crash mid-write cannot leave the
credential file truncated.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MEMORY_SECTIONS = ("persona", "relations", "positions", "bars", "notes")

# Cross-target duplicate detection only matters while the source content is
# still publicly visible, which the platform defines as the last 24 hours.
FINGERPRINT_TTL_SECONDS = 24 * 3600
MAX_FINGERPRINTS = 400
MAX_IMAGE_RECORDS = 300
MAX_RUNS = 20


def mask_key(value: str | None) -> str:
    """Render an api_key so it is recognisable but never usable."""

    if not value:
        return "(未设置)"
    text = str(value)
    if len(text) <= 10:
        return text[:2] + "***"
    return f"{text[:8]}...{text[-4:]} (len={len(text)})"


@dataclass
class Credentials:
    """The single-origin credential set for aitaolun.net."""

    api_key: str = ""
    agent_name: str = ""
    claim_url: str = ""
    claimed: bool = False
    registered_at: float = 0.0
    framework: str = ""

    @property
    def has_key(self) -> bool:
        return bool(self.api_key.strip())

    def public_dict(self) -> dict[str, Any]:
        """Serialisable view with the key masked, safe for chat and logs."""

        return {
            "agent_name": self.agent_name or "(未注册)",
            "api_key": mask_key(self.api_key),
            "claimed": self.claimed,
            "claim_url_saved": bool(self.claim_url),
            "registered_at": self.registered_at,
        }


@dataclass
class Cooldown:
    """A locally remembered server-imposed pause for one class of writes."""

    kind: str
    until: float
    reason: str = ""

    @property
    def remaining(self) -> int:
        return max(0, int(round(self.until - time.time())))


@dataclass
class RunRecord:
    """One heartbeat / manual run summary kept for the runs command."""

    started_at: float
    trigger: str
    status: str
    detail: str = ""
    session: str = ""


@dataclass
class DuplicateHit:
    """A previous write whose canonical content matches a new, other target."""

    kind: str
    target: str
    created_at: float
    result_id: str = ""


@dataclass
class StateStore:
    """Owns every private file this plugin writes."""

    data_dir: Path
    _credentials: Credentials | None = field(default=None, init=False, repr=False)
    _memory: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _runtime: dict[str, Any] | None = field(default=None, init=False, repr=False)

    # ---------------------------------------------------------------- files

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"

    @property
    def runtime_path(self) -> Path:
        return self.data_dir / "runtime.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_json(
        self, path: Path, data: dict[str, Any], private: bool = False
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if private:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)

    # ---------------------------------------------------------- credentials

    def credentials(self) -> Credentials:
        if self._credentials is None:
            data = self._read_json(self.credentials_path)
            self._credentials = Credentials(
                api_key=str(data.get("api_key") or ""),
                agent_name=str(data.get("agent_name") or ""),
                claim_url=str(data.get("claim_url") or ""),
                claimed=bool(data.get("claimed") or False),
                registered_at=float(data.get("registered_at") or 0.0),
                framework=str(data.get("framework") or ""),
            )
        return self._credentials

    def save_credentials(self, creds: Credentials) -> None:
        self._credentials = creds
        self._write_json(
            self.credentials_path,
            {
                "api_key": creds.api_key,
                "agent_name": creds.agent_name,
                "claim_url": creds.claim_url,
                "claimed": creds.claimed,
                "registered_at": creds.registered_at,
                "framework": creds.framework,
            },
            private=True,
        )

    def set_api_key(self, api_key: str, agent_name: str = "") -> None:
        creds = self.credentials()
        creds.api_key = api_key.strip()
        if agent_name:
            creds.agent_name = agent_name.strip()
        if not creds.registered_at:
            creds.registered_at = time.time()
        self.save_credentials(creds)

    def clear_credentials(self) -> None:
        self.save_credentials(Credentials())

    def mark_claimed(self, claimed: bool = True) -> None:
        creds = self.credentials()
        if creds.claimed != claimed:
            creds.claimed = claimed
            self.save_credentials(creds)

    def forget_claim_url(self) -> None:
        """Drop the one-time claim link once the owner reports it is used."""

        creds = self.credentials()
        if creds.claim_url:
            creds.claim_url = ""
            self.save_credentials(creds)

    # --------------------------------------------------------------- memory

    def memory(self) -> dict[str, Any]:
        if self._memory is None:
            data = self._read_json(self.memory_path)
            sections = data.get("sections")
            if not isinstance(sections, dict):
                sections = {}
            self._memory = {
                "sections": {
                    name: str(sections.get(name) or "") for name in MEMORY_SECTIONS
                },
                "updated_at": data.get("updated_at") or {},
            }
        return self._memory

    def read_memory(self, section: str | None = None) -> dict[str, str]:
        sections = dict(self.memory()["sections"])
        if section:
            key = section.strip().lower()
            if key not in MEMORY_SECTIONS:
                raise KeyError(section)
            return {key: sections.get(key, "")}
        return sections

    def write_memory(self, section: str, text: str, append: bool = False) -> str:
        key = section.strip().lower()
        if key not in MEMORY_SECTIONS:
            raise KeyError(section)
        mem = self.memory()
        current = mem["sections"].get(key, "")
        if append and current.strip():
            merged = current.rstrip() + "\n" + text.strip()
        else:
            merged = text.strip()
        mem["sections"][key] = merged
        updated = mem.get("updated_at")
        if not isinstance(updated, dict):
            updated = {}
        updated[key] = time.time()
        mem["updated_at"] = updated
        self._write_json(self.memory_path, mem)
        return merged

    def memory_updated_at(self) -> dict[str, float]:
        updated = self.memory().get("updated_at")
        if not isinstance(updated, dict):
            return {}
        return {
            str(key): float(value)
            for key, value in updated.items()
            if isinstance(value, (int, float))
        }

    # -------------------------------------------------------------- runtime

    def runtime(self) -> dict[str, Any]:
        if self._runtime is None:
            data = self._read_json(self.runtime_path)
            data.setdefault("cooldowns", {})
            data.setdefault("banned", {})
            data.setdefault("fingerprints", [])
            data.setdefault("images", [])
            data.setdefault("scheduler", {})
            data.setdefault("skill_update", {})
            data.setdefault("runs", [])
            data.setdefault("session", {})
            self._runtime = data
        return self._runtime

    def _save_runtime(self) -> None:
        self._write_json(self.runtime_path, self.runtime())

    # cooldowns -----------------------------------------------------------

    def set_cooldown(self, kind: str, seconds: int, reason: str = "") -> Cooldown:
        seconds = max(1, int(seconds))
        cooldown = Cooldown(kind=kind, until=time.time() + seconds, reason=reason)
        self.runtime()["cooldowns"][kind] = {
            "until": cooldown.until,
            "reason": reason,
        }
        self._save_runtime()
        return cooldown

    def cooldown(self, kind: str) -> Cooldown | None:
        entry = self.runtime()["cooldowns"].get(kind)
        if not isinstance(entry, dict):
            return None
        until = float(entry.get("until") or 0.0)
        if until <= time.time():
            return None
        return Cooldown(kind=kind, until=until, reason=str(entry.get("reason") or ""))

    def active_cooldowns(self) -> list[Cooldown]:
        result = []
        for kind in list(self.runtime()["cooldowns"]):
            cooldown = self.cooldown(kind)
            if cooldown is not None:
                result.append(cooldown)
        return sorted(result, key=lambda item: item.until)

    def clear_cooldown(self, kind: str) -> None:
        if self.runtime()["cooldowns"].pop(kind, None) is not None:
            self._save_runtime()

    # platform ban latch --------------------------------------------------

    def set_platform_banned(self, banned: bool, reason: str = "") -> None:
        self.runtime()["banned"] = (
            {"at": time.time(), "reason": reason} if banned else {}
        )
        self._save_runtime()

    def platform_ban(self) -> dict[str, Any] | None:
        entry = self.runtime().get("banned")
        return entry if isinstance(entry, dict) and entry else None

    # write fingerprints --------------------------------------------------

    def _prune_fingerprints(self) -> list[dict[str, Any]]:
        cutoff = time.time() - FINGERPRINT_TTL_SECONDS
        items = [
            item
            for item in self.runtime()["fingerprints"]
            if isinstance(item, dict)
            and float(item.get("created_at") or 0.0) >= cutoff
        ]
        if len(items) > MAX_FINGERPRINTS:
            items = items[-MAX_FINGERPRINTS:]
        self.runtime()["fingerprints"] = items
        return items

    def find_cross_target_duplicate(
        self, kind: str, target: str, fingerprint: str
    ) -> DuplicateHit | None:
        """Return a previous same-content write aimed at a *different* target.

        Same target + same content is a legal exact retry, so it is not a hit.
        Different target + same content is exactly what the platform punishes
        with DUPLICATE_CONTENT and escalating bans, so we stop it locally.
        """

        for item in reversed(self._prune_fingerprints()):
            if item.get("kind") != kind or item.get("fingerprint") != fingerprint:
                continue
            if str(item.get("target") or "") == target:
                continue
            return DuplicateHit(
                kind=kind,
                target=str(item.get("target") or ""),
                created_at=float(item.get("created_at") or 0.0),
                result_id=str(item.get("result_id") or ""),
            )
        return None

    def record_write(
        self, kind: str, target: str, fingerprint: str, result_id: str = ""
    ) -> None:
        self._prune_fingerprints().append(
            {
                "kind": kind,
                "target": target,
                "fingerprint": fingerprint,
                "result_id": result_id,
                "created_at": time.time(),
            }
        )
        self._save_runtime()

    # image attribution ---------------------------------------------------

    def record_image(self, path: str, source: str = "") -> None:
        images = [
            item
            for item in self.runtime()["images"]
            if isinstance(item, dict) and item.get("path") != path
        ]
        images.append({"path": path, "source": source, "at": time.time()})
        self.runtime()["images"] = images[-MAX_IMAGE_RECORDS:]
        self._save_runtime()

    def owns_image(self, path: str) -> bool:
        return any(
            isinstance(item, dict) and item.get("path") == path
            for item in self.runtime()["images"]
        )

    def owned_images(self) -> list[dict[str, Any]]:
        return [item for item in self.runtime()["images"] if isinstance(item, dict)]

    # scheduler / skill update -------------------------------------------

    def scheduler_state(self) -> dict[str, Any]:
        state = self.runtime()["scheduler"]
        return state if isinstance(state, dict) else {}

    def update_scheduler_state(self, **values: Any) -> dict[str, Any]:
        state = self.scheduler_state()
        state.update(values)
        self.runtime()["scheduler"] = state
        self._save_runtime()
        return state

    def skill_update_state(self) -> dict[str, Any]:
        state = self.runtime()["skill_update"]
        return state if isinstance(state, dict) else {}

    def update_skill_update_state(self, **values: Any) -> dict[str, Any]:
        state = self.skill_update_state()
        state.update(values)
        self.runtime()["skill_update"] = state
        self._save_runtime()
        return state

    # bound owner session -------------------------------------------------

    def bound_session(self) -> str:
        session = self.runtime()["session"]
        if isinstance(session, dict):
            return str(session.get("umo") or "")
        return ""

    def bind_session(self, umo: str, label: str = "") -> None:
        self.runtime()["session"] = {"umo": umo, "label": label, "at": time.time()}
        self._save_runtime()

    def unbind_session(self) -> None:
        self.runtime()["session"] = {}
        self._save_runtime()

    # run history ---------------------------------------------------------

    def append_run(self, record: RunRecord) -> None:
        runs = [item for item in self.runtime()["runs"] if isinstance(item, dict)]
        runs.append(
            {
                "started_at": record.started_at,
                "trigger": record.trigger,
                "status": record.status,
                "detail": record.detail,
                "session": record.session,
            }
        )
        self.runtime()["runs"] = runs[-MAX_RUNS:]
        self._save_runtime()

    def runs(self, limit: int = MAX_RUNS) -> list[RunRecord]:
        items = [item for item in self.runtime()["runs"] if isinstance(item, dict)]
        selected = items[-max(1, limit):]
        return [
            RunRecord(
                started_at=float(item.get("started_at") or 0.0),
                trigger=str(item.get("trigger") or ""),
                status=str(item.get("status") or ""),
                detail=str(item.get("detail") or ""),
                session=str(item.get("session") or ""),
            )
            for item in reversed(selected)
        ]
