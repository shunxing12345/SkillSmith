"""SkillStore — 技能库持久化封装

磁盘 SKILL.md 持久化 + local_cache + DB 元数据同步。

    Agent  ←→  Provider  ←→  SkillStore
                                ├── persistence (磁盘 SKILL.md I/O)
                                └── SkillService (DB 元数据)
"""

from __future__ import annotations

import json
import shutil
from difflib import unified_diff
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from middleware.config import g_config
from utils.logger import get_logger
from core.skill.execution.analyzer.parsing import validate_skill_md
from core.skill.schema import Skill
from core.skill.store.persistence import (
    load_all_skills,
    load_skill_from_dir,
    save_skill_to_disk,
)

logger = get_logger(__name__)


class SkillStore:
    """技能库：磁盘持久化 + local_cache + DB 元数据

    目录结构:
        workspace/
        ├── skills/
        │   ├── get-weather-mock/
        │   │   ├── SKILL.md
        │   │   └── scripts/
        │   │       └── get_weather_mock.py
        │   └── ...
        ├── data/memento_s.db
    """

    def __init__(
        self,
        skill_service: Any | None = None,
        embedding_client=None,
    ):
        self._skill_service = skill_service  # middleware SkillService (async)
        self._embedding_client = embedding_client

        self.skills_directory = g_config.get_skills_path()
        self.candidates_directory = self.skills_directory / ".candidates"
        self.history_directory = self.skills_directory / ".history"
        self.local_cache: dict[str, Skill] = load_all_skills(self.skills_directory)
        logger.info(
            f"SkillStore init: skills_dir={self.skills_directory}, "
            f"local_cache={len(self.local_cache)}, "
            f"names={sorted(self.local_cache.keys())}"
        )

    # ── Public API ────────────────────────────────────────────────

    async def add_skill(self, skill: Skill) -> None:
        """注册技能到磁盘 + 内存缓存 + DB

        Args:
            skill: 技能对象
        """
        embedding = await self._embed_skill(skill)
        save_skill_to_disk(skill, self.skills_directory)
        self.local_cache[skill.name] = skill
        await self._upsert_to_db(skill, embedding=embedding)
        logger.info("Skill stored: {}", skill.name)

    async def record_success_example(
        self,
        *,
        skill_name: str,
        request: str,
        summary: str,
        execution_status: str = "success",
        task_status: str = "uncertain",
        verification_status: str = "unverified",
        confidence: float = 0.35,
        feedback_source: str = "runtime",
        feedback_note: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Persist a structured successful execution example for regression replay.

        Note:
            "successful" here means execution reached a non-error envelope.
            It does not imply the final user-visible task result is verified.
        """
        self.history_directory.mkdir(parents=True, exist_ok=True)
        history_path = self.history_directory / f"{skill_name}.jsonl"

        records: list[dict[str, Any]] = []
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue

        entry = {
            "request": request,
            "summary": summary,
            "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "execution_status": execution_status,
            "task_status": task_status,
            "verification_status": verification_status,
            "confidence": confidence,
            "feedback_source": feedback_source,
            "feedback_note": feedback_note,
        }
        records = [
            r for r in records
            if str(r.get("request", "")).strip() != request.strip()
        ]
        records.append(entry)
        records = records[-limit:]

        history_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "path": str(history_path), "count": len(records)}

    def get_recent_success_examples(
        self,
        *,
        skill_name: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Load recent successful execution examples for a skill."""
        history_path = self.history_directory / f"{skill_name}.jsonl"
        if not history_path.exists():
            return []

        records: list[dict[str, Any]] = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
        filtered = [
            r for r in records
            if str(r.get("execution_status", "success")) == "success"
            and str(r.get("verification_status", "unverified")) != "user_rejected"
        ]
        return filtered[-limit:]

    async def store_candidate(
        self,
        *,
        skill: Skill,
        updated_skill_md: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a candidate skill revision without replacing the live skill."""
        source_dir = Path(skill.source_dir or "")
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Skill source dir missing: {source_dir}")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate_root = self.candidates_directory / source_dir.name
        candidate_dir = candidate_root / timestamp
        candidate_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, candidate_dir)

        skill_md_path = candidate_dir / "SKILL.md"
        skill_md_path.write_text(updated_skill_md, encoding="utf-8")

        candidate_skill = load_skill_from_dir(candidate_dir)
        meta = {
            "skill_name": skill.name,
            "parent_version": skill.version,
            "created_at": timestamp,
        }
        if metadata:
            meta.update(metadata)
        (candidate_dir / "candidate.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        logger.info("Stored candidate skill revision: {}", candidate_dir)
        return {
            "path": str(candidate_dir),
            "skill_name": candidate_skill.name,
            "parent_skill": skill.name,
            "created_at": timestamp,
        }

    async def verify_candidate(
        self,
        *,
        candidate_path: str,
        parent_skill_name: str,
    ) -> dict[str, Any]:
        """Run a minimal static verification pass for a candidate skill."""
        candidate_dir = Path(candidate_path)
        skill_md_path = candidate_dir / "SKILL.md"
        if not skill_md_path.exists():
            return {"ok": False, "reason": "missing_skill_md"}

        content = skill_md_path.read_text(encoding="utf-8")
        if not validate_skill_md(content):
            return {"ok": False, "reason": "invalid_skill_md"}

        try:
            loaded = load_skill_from_dir(candidate_dir)
        except Exception as e:
            return {"ok": False, "reason": f"load_failed: {e}"}

        if loaded.name != parent_skill_name:
            return {
                "ok": False,
                "reason": f"name_changed: {loaded.name} != {parent_skill_name}",
            }

        return {
            "ok": True,
            "reason": "static_validation_passed",
            "skill_name": loaded.name,
        }

    def load_candidate(self, candidate_path: str) -> Skill:
        """Load a candidate skill from disk for replay verification."""
        return load_skill_from_dir(Path(candidate_path))

    def list_candidates(self) -> list[dict[str, Any]]:
        """List stored candidate revisions with metadata."""
        if not self.candidates_directory.exists():
            return []

        records: list[dict[str, Any]] = []
        for skill_dir in sorted(self.candidates_directory.iterdir()):
            if not skill_dir.is_dir():
                continue
            for candidate_dir in sorted(skill_dir.iterdir(), reverse=True):
                if not candidate_dir.is_dir():
                    continue
                meta_path = candidate_dir / "candidate.json"
                meta: dict[str, Any] = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except Exception:
                        meta = {}
                records.append(
                    {
                        "path": str(candidate_dir),
                        "skill_name": meta.get("skill_name") or skill_dir.name,
                        "created_at": meta.get("created_at") or candidate_dir.name,
                        "status": meta.get("status") or "candidate",
                        "patch_summary": meta.get("patch_summary") or "",
                        "rejection_reason": meta.get("rejection_reason") or "",
                    }
                )
        return records

    def get_candidate_details(self, candidate_path: str) -> dict[str, Any]:
        """Return detailed candidate metadata plus current SKILL.md content."""
        candidate_dir = Path(candidate_path)
        meta_path = candidate_dir / "candidate.json"
        skill_md_path = candidate_dir / "SKILL.md"
        if not skill_md_path.exists():
            raise FileNotFoundError(f"Candidate SKILL.md not found: {skill_md_path}")

        meta: dict[str, Any] = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        return {
            "path": str(candidate_dir),
            "metadata": meta,
            "skill_md": skill_md_path.read_text(encoding="utf-8"),
        }

    def diff_candidate(self, candidate_path: str) -> dict[str, Any]:
        """Compute unified diff between live skill and candidate SKILL.md."""
        candidate_dir = Path(candidate_path)
        meta_path = candidate_dir / "candidate.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Missing candidate metadata: {meta_path}")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parent_skill_name = str(meta.get("skill_name") or "")
        if not parent_skill_name:
            raise ValueError("candidate metadata missing skill_name")

        live_skill = self.find_by_name(parent_skill_name)
        if live_skill is None or not live_skill.source_dir:
            raise FileNotFoundError(f"Live skill not found for candidate: {parent_skill_name}")

        live_skill_md = Path(live_skill.source_dir) / "SKILL.md"
        candidate_skill_md = candidate_dir / "SKILL.md"
        live_text = live_skill_md.read_text(encoding="utf-8").splitlines(keepends=True)
        candidate_text = candidate_skill_md.read_text(encoding="utf-8").splitlines(keepends=True)
        diff = "".join(
            unified_diff(
                live_text,
                candidate_text,
                fromfile=str(live_skill_md),
                tofile=str(candidate_skill_md),
            )
        )
        return {
            "skill_name": parent_skill_name,
            "candidate_path": str(candidate_dir),
            "live_path": str(live_skill_md),
            "diff": diff,
        }

    async def promote_candidate(self, *, candidate_path: str) -> dict[str, Any]:
        """Promote a candidate SKILL.md into the live skill directory."""
        candidate_dir = Path(candidate_path)
        meta_path = candidate_dir / "candidate.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Missing candidate metadata: {meta_path}")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parent_skill_name = str(meta.get("skill_name") or "")
        if not parent_skill_name:
            raise ValueError("candidate metadata missing skill_name")

        live_skill = self.find_by_name(parent_skill_name)
        if live_skill is None or not live_skill.source_dir:
            raise FileNotFoundError(f"Live skill not found for candidate: {parent_skill_name}")

        candidate_skill_md = candidate_dir / "SKILL.md"
        live_skill_dir = Path(live_skill.source_dir)
        live_skill_md = live_skill_dir / "SKILL.md"
        shutil.copy2(candidate_skill_md, live_skill_md)

        updated = load_skill_from_dir(live_skill_dir)
        updated.version = live_skill.version + 1
        self.local_cache[updated.name] = updated
        await self._upsert_to_db(updated)

        meta["status"] = "promoted"
        meta["promoted_at"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Promoted candidate '{}' into live skill '{}'", candidate_dir, updated.name)
        return {
            "ok": True,
            "skill_name": updated.name,
            "path": str(live_skill_dir),
            "version": updated.version,
        }

    async def reject_candidate(self, *, candidate_path: str, reason: str) -> dict[str, Any]:
        """Mark a candidate as rejected without deleting it."""
        candidate_dir = Path(candidate_path)
        meta_path = candidate_dir / "candidate.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta["status"] = "rejected"
        meta["rejected_at"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        meta["rejection_reason"] = reason
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Rejected candidate '{}': {}", candidate_dir, reason)
        return {"ok": True, "path": str(candidate_dir), "reason": reason}

    async def remove_skill(self, skill_name: str) -> bool:
        """从技能库完全删除一个 skill

        删除内容:
            1. 文件系统: skills/<name>/ 目录
            2. 内存缓存: local_cache 中移除
            3. DB: skills 表记录
        """
        if skill_name not in self.local_cache:
            return False

        for dirname in [skill_name, skill_name.replace("_", "-")]:
            skill_dir = self.skills_directory / dirname
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
                logger.info("Removed skill directory: {}", skill_dir)
                break

        del self.local_cache[skill_name]

        await self._delete_from_db(skill_name)

        logger.info("Skill '{}' removed", skill_name)
        return True

    async def refresh_from_disk(self) -> int:
        """增量扫描 skills/ 目录，将磁盘上新增的 skill 加载到 local_cache + DB。

        仅加载 local_cache 中尚不存在的目录。
        返回新增 skill 数量。
        """
        added = 0
        if not self.skills_directory.exists():
            return added

        for skill_dir in sorted(self.skills_directory.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
                continue
            try:
                skill = load_skill_from_dir(skill_dir)
                if skill.name not in self.local_cache:
                    self.local_cache[skill.name] = skill
                    await self._upsert_to_db(skill)
                    added += 1
                    logger.info("Hot-loaded new skill from disk: {}", skill.name)
            except Exception as e:
                logger.debug("refresh_from_disk: skip '{}': {}", skill_dir.name, e)

        if added:
            logger.info("refresh_from_disk: {} new skill(s) added", added)

        return added

    async def cleanup_orphaned_skills(self, indexer=None) -> list[str]:
        """清理磁盘已删除但 DB/向量库残留的孤儿 skill。"""
        if not self._skill_service:
            logger.debug("No skill service available, skipping orphan cleanup")
            return []

        try:
            db_skill_names = await self._skill_service.list_all_names()
            orphans = db_skill_names - set(self.local_cache.keys())

            if not orphans:
                logger.debug("No orphaned skills found in DB")
                return []

            cleaned = []
            for skill_name in orphans:
                try:
                    if indexer and indexer.is_ready:
                        indexer.delete(skill_name)
                        logger.debug("Deleted embedding for skill: {}", skill_name)
                    await self._delete_from_db(skill_name)
                    cleaned.append(skill_name)
                    logger.info("Cleaned up orphaned skill: {}", skill_name)
                except Exception as e:
                    logger.warning(
                        "Failed to clean up orphaned skill '{}': {}", skill_name, e
                    )

            if cleaned:
                logger.info(
                    "Cleanup complete: {} orphaned skill(s) removed", len(cleaned)
                )
            return cleaned

        except Exception as e:
            logger.warning("Failed to cleanup orphaned skills: {}", e)
            return []

    async def sync_all_to_db(self):
        """启动时将所有 local_cache 中的 skill 同步到 DB

        幂等操作：已存在的更新描述，不存在的创建。
        """
        if not self._skill_service or not self.local_cache:
            return

        synced = 0
        for skill in self.local_cache.values():
            try:
                await self._upsert_to_db(skill)
                synced += 1
            except Exception as e:
                logger.debug("sync_all_to_db: skip '{}': {}", skill.name, e)

        if synced:
            logger.info("Synced {} skill(s) to DB", synced)

    def find_by_name(self, name: str) -> Skill | None:
        """按名称查找 skill，支持多种命名格式。

        支持 snake_case、kebab-case、以及带连字符的变体。

        Args:
            name: skill 名称（支持多种格式）

        Returns:
            Skill 对象或 None
        """
        from core.skill.store.persistence import to_kebab_case

        # 标准化：统一为 snake_case
        normalized = name.replace("-", "_")
        if normalized in self.local_cache:
            return self.local_cache[normalized]

        # 尝试 kebab-case 变体
        alt = to_kebab_case(name).replace("-", "_")
        if alt in self.local_cache:
            return self.local_cache[alt]

        return None

    async def load_from_path(self, path: Path) -> Skill:
        """从指定路径加载 skill 并添加到缓存。

        用于云端下载后加载 skill 到本地存储。

        Args:
            path: skill 目录路径

        Returns:
            加载的 Skill 对象

        Raises:
            FileNotFoundError: 如果 SKILL.md 不存在
            ValueError: 如果解析失败
        """
        from core.skill.store.persistence import load_skill_from_dir

        skill = load_skill_from_dir(path)
        self.local_cache[skill.name] = skill
        await self._upsert_to_db(skill)
        logger.info("Skill loaded from path and added to cache: {}", skill.name)
        return skill

    # ── Internal: DB ─────────────────────────────────────────────

    async def _upsert_to_db(self, skill: Skill, embedding: bytes | None = None):
        """写入或更新 DB 元数据"""
        if not self._skill_service:
            return

        try:
            from middleware.storage.schemas import SkillCreate, SkillUpdate

            existing = await self._skill_service.get_by_name(skill.name)
            if existing:
                await self._skill_service.update(
                    existing.id,
                    SkillUpdate(
                        description=skill.description,
                        version=str(skill.version),
                    ),
                )
            else:
                await self._skill_service.create(
                    SkillCreate(
                        name=skill.name,
                        description=skill.description,
                        version=str(skill.version),
                        source_type="local",
                        local_path=str(skill.source_dir or ""),
                        embedding=embedding,
                    )
                )
        except Exception as e:
            logger.warning("DB upsert failed for '{}': {}", skill.name, e)

    async def _delete_from_db(self, skill_name: str):
        """从 DB 删除元数据"""
        if not self._skill_service:
            return

        try:
            existing = await self._skill_service.get_by_name(skill_name)
            if existing:
                await self._skill_service.delete(existing.id)
        except Exception as e:
            logger.warning("DB delete failed for '{}': {}", skill_name, e)

    async def _embed_skill(self, skill: Skill) -> bytes | None:
        if not self._embedding_client:
            return None
        try:
            vecs = await self._embedding_client.embed([skill.to_embedding_text()])
            if not vecs:
                return None
            from core.skill.embedding.utils import serialize_f32

            return serialize_f32(vecs[0])
        except Exception as e:
            logger.warning("Skill embedding failed for '{}': {}", skill.name, e)
            return None
