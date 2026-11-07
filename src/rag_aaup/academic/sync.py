import hashlib
import json
import shutil
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from rag_aaup.academic.domain import parse_curriculum
from rag_aaup.academic.references import build_reference, export_images
from rag_aaup.config import PROJECT_ROOT


class AcademicReferences:
    """Curated public sources only; local answers never fetch the website at question time."""

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        self.state_dir = root / "data" / "academic_sources"
        self.output_dir = root / "output" / "pdf"
        self.config_path = root / "configs" / "aaup_study_plans.json"
        self._lock = threading.Lock()
        self.is_syncing = False

    def state(self) -> dict:
        path = self.state_dir / "manifest.json"
        return (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"programs": {}, "errors": {}}
        )

    def sync(self, force: bool = False, cached_html: Path | None = None) -> dict:
        with self._lock:
            state = self.state()
            if not force and cached_html is None and time.time() - state.get("last_attempt_epoch", 0) < 3600:
                return state
            self.is_syncing = True
            try:
                return self._sync(state, cached_html)
            finally:
                self.is_syncing = False

    def _sync(self, state: dict, cached_html: Path | None) -> dict:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        state["last_attempt_epoch"] = time.time()
        state["errors"] = {}
        user_agent = "CampusIQ-ReferenceSync/1.0"
        with httpx.Client(timeout=35, follow_redirects=False, headers={"User-Agent": user_agent}) as client:
            robots = RobotFileParser()
            if cached_html is None:
                try:
                    response = client.get("https://www.aaup.edu/robots.txt")
                    response.raise_for_status()
                    if "user-agent" not in response.text.lower():
                        raise ValueError("robots.txt is not a recognizable robots policy")
                    robots.parse(response.text.splitlines())
                except (httpx.HTTPError, ValueError) as error:
                    state["errors"]["robots"] = type(error).__name__
                    for record in state["programs"].values():
                        record["verification_status"] = "cached_check_failed"
                    self._save_state(state)
                    return state
            else:
                robots.parse((cached_html / "robots.txt").read_text(encoding="utf-8").splitlines())
            for source in config["sources"]:
                key = source["major_id"]
                url = source["url"]
                try:
                    if urlparse(url).hostname not in config["allowed_hosts"] or not robots.can_fetch(
                        user_agent, url
                    ):
                        raise ValueError("Source is outside the approved hosts or disallowed by robots.txt")
                    if cached_html is not None:
                        aliases = {
                            "computer_networks": "networks",
                            "computer_science": "cs",
                            "ai_robotics": "ai_robotics",
                        }
                        content = (cached_html / f"{aliases[key]}.html").read_bytes()
                    else:
                        time.sleep(max(1, robots.crawl_delay(user_agent) or 1))
                        response = client.get(url)
                        response.raise_for_status()
                        content = response.content
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("Source exceeded the configured size limit")
                    plan = parse_curriculum(content, source)
                    digest = plan.semantic_hash()
                    previous = state["programs"].get(key, {})
                    pdf = self.output_dir / f"{plan.slug}_Reference.pdf"
                    changed = previous.get("content_hash") != digest or not pdf.exists()
                    if changed:
                        archive = self.state_dir / "history" / key
                        archive.mkdir(parents=True, exist_ok=True)
                        (archive / f"{digest}.html").write_bytes(content)
                        (archive / f"{digest}.json").write_text(
                            json.dumps(asdict(plan), ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        payload = build_reference(plan, pdf, timestamp[:10])
                        images = export_images(pdf, payload, self.output_dir / "images" / plan.slug)
                    else:
                        images = sorted((self.output_dir / "images" / plan.slug).glob("page-*.png"))
                    state["programs"][key] = {
                        "major_id": key,
                        "title": plan.title,
                        "source_url": url,
                        "required_credits": plan.required_credits,
                        "content_hash": digest,
                        "file": pdf.relative_to(self.root).as_posix(),
                        "images": [path.relative_to(self.root).as_posix() for path in images],
                        "last_verified_at": timestamp,
                        "publication_date": None,
                        "effective_date": None,
                        "admission_status": "not_verified_from_curriculum",
                        "verification_status": "verified",
                        "changed": changed,
                    }
                except (httpx.HTTPError, ValueError, OSError) as error:
                    state["errors"][key] = f"{type(error).__name__}: {error}"
                    if key in state["programs"]:
                        state["programs"][key]["verification_status"] = "cached_check_failed"
        if not state["errors"]:
            state["last_successful_sync"] = timestamp
        self._save_state(state)
        return state

    def _save_state(self, state: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_dir / ".manifest.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_dir / "manifest.json")

    def install(self, directory: Path) -> list[str]:
        directory.mkdir(parents=True, exist_ok=True)
        copied = []
        for record in self.state()["programs"].values():
            source = self.root / record["file"]
            destination = directory / source.name
            if (
                destination.exists()
                and hashlib.sha256(destination.read_bytes()).digest()
                == hashlib.sha256(source.read_bytes()).digest()
            ):
                continue
            temporary = directory / f".{source.name}.installing"
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            copied.append(destination.name)
        return copied
