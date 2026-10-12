"""AIONA 특화 평가 설정 연결.

- 내부 마커 목록: ``code_eval/settings/aiona_service_rules_v1.json`` 의 ``u8_1`` 을 그대로 읽는다.
  (현재 저장소의 운영 목록은 비어 있다 — ``pending_registration`` 에 범주 이름만 있다.)
- 질문별 금지 표현: ``forbidden_expressions_path`` 의 JSON. 저장소에 실제 운영 목록은 없다.
- 설명용·검증용 가상 문자열(canary)을 운영 목록으로 쓰지 않는다. 테스트는 테스트 안에서 설정을 만든다.

설정이 없거나 비어 있으면 검출 0건(PASS)으로 바꾸지 않고 CONFIG_MISSING 으로 남긴다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import nfc
from .model import stable_hash
from .rule_pattern import RegistryError, RulePattern

__all__ = ["ROOT", "DEFAULT_CONFIG_PATH", "MARKER_CATEGORIES", "AionaConfig", "load_aiona_config",
           "question_sha256", "ConfigError"]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "code_eval" / "settings" / "aiona_specialized.json"

#: MD 155행이 나열한 여섯 범주. 모두 등록돼야 '전체 적용 범위 검사 완료'다.
MARKER_CATEGORIES = {
    "viz_placeholder": "시각화 자리표시자",
    "general_placeholder": "일반 자리표시자",
    "internal_section_tag": "내부 구간 태그",
    "tool_artifact_tag": "도구/아티팩트 태그",
    "followup_tag": "후속질문 태그",
    "citation_residue": "인용 번호 2개 이상 연속 잔재",
}


class ConfigError(ValueError):
    """설정 파일 결함. 답변 결함이 아니다."""


def question_sha256(question: str) -> str:
    return hashlib.sha256(nfc(question).strip().encode("utf-8")).hexdigest()


@dataclass
class MarkerPattern:
    rule: RulePattern
    category: Optional[str]


@dataclass
class AionaConfig:
    enabled: bool = True
    applies_to_system_ids: Tuple[str, ...] = ("aiona",)
    marker_patterns: List[MarkerPattern] = field(default_factory=list)
    marker_source: str = ""
    marker_status: str = ""
    forbidden_entries: Optional[List[Dict[str, Any]]] = None
    forbidden_source: str = ""
    errors: List[str] = field(default_factory=list)
    raw_hash: str = ""

    def applies_to(self, system_id: Optional[str]) -> Optional[bool]:
        if not system_id:
            return None
        sid = system_id.strip().lower()
        return any(tag in sid for tag in self.applies_to_system_ids)

    @property
    def covered_categories(self) -> List[str]:
        return sorted({p.category for p in self.marker_patterns if p.category in MARKER_CATEGORIES})

    @property
    def missing_categories(self) -> List[str]:
        return [c for c in MARKER_CATEGORIES if c not in self.covered_categories]

    def summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "applies_to_system_ids": list(self.applies_to_system_ids),
            "marker_source": self.marker_source,
            "marker_registry_status": self.marker_status,
            "marker_patterns": len(self.marker_patterns),
            "marker_categories_covered": self.covered_categories,
            "marker_categories_missing": [MARKER_CATEGORIES[c] for c in self.missing_categories],
            "forbidden_source": self.forbidden_source,
            "forbidden_entries": None if self.forbidden_entries is None else len(self.forbidden_entries),
            "errors": list(self.errors),
            "config_hash": self.raw_hash,
        }


def _marker_patterns(registry: Dict[str, Any], groups: List[str], where: str) -> List[MarkerPattern]:
    section = registry.get("u8_1") or {}
    if section.get("enabled") is False:
        return []
    found: List[MarkerPattern] = []
    for group in groups:
        items = section.get(group) or []
        if not isinstance(items, list):
            raise ConfigError(f"{where}: u8_1.{group} 은 목록이어야 합니다")
        for i, item in enumerate(items):
            try:
                rule = RulePattern.from_dict(item, f"{where}: u8_1.{group}[{i}]")
            except RegistryError as exc:
                raise ConfigError(str(exc)) from exc
            category = item.get("category") if isinstance(item, dict) else None
            if category is not None and category not in MARKER_CATEGORIES:
                raise ConfigError(f"{where}: 알 수 없는 마커 범주 {category!r}")
            found.append(MarkerPattern(rule, category))
    return found


def _forbidden_entries(payload: Any, where: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ConfigError(f"{where}: {{'entries': [...]}} 형식이어야 합니다")
    entries = []
    for i, e in enumerate(payload["entries"]):
        if not isinstance(e, dict) or not isinstance(e.get("case_id"), str):
            raise ConfigError(f"{where}: entries[{i}].case_id 가 필요합니다")
        if not (isinstance(e.get("question_sha256"), str) or isinstance(e.get("question"), str)):
            raise ConfigError(f"{where}: entries[{i}] 에 question 또는 question_sha256 이 필요합니다")
        exprs = e.get("forbidden_expressions")
        if not isinstance(exprs, list) or any(not isinstance(x, str) or not x.strip() for x in exprs):
            raise ConfigError(f"{where}: entries[{i}].forbidden_expressions 는 비어 있지 않은 문자열 목록이어야 합니다")
        entries.append(e)
    return entries


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_aiona_config(path: Optional[Path] = None, override: Optional[Dict[str, Any]] = None) -> AionaConfig:
    """설정 파일(기본 경로)을 읽는다. ``override`` 는 CLI·화면에서 올린 설정 객체다.

    override 형식: {"applies_to_system_ids": [...], "marker_registry": {레지스트리 객체},
                    "forbidden_expressions": {"entries": [...]}}
    """
    cfg = AionaConfig()
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    base: Dict[str, Any] = {}
    try:
        base = _read_json(path)
    except FileNotFoundError:
        cfg.errors.append(f"설정 파일 없음: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        cfg.errors.append(f"설정 파일 오류: {path}: {exc}")
    if base.get("applies_to_system_ids"):
        cfg.applies_to_system_ids = tuple(str(x).lower() for x in base["applies_to_system_ids"])
    groups = base.get("marker_registry_groups") or ["internal_marker_patterns", "internal_tag_patterns"]

    registry: Optional[Dict[str, Any]] = None
    reg_path = base.get("marker_registry_path")
    if reg_path:
        full = (ROOT / reg_path).resolve()
        try:
            registry = _read_json(full)
            cfg.marker_source = reg_path
            cfg.marker_status = str(registry.get("status", ""))
        except (OSError, json.JSONDecodeError) as exc:
            cfg.errors.append(f"마커 레지스트리를 읽지 못함: {reg_path}: {exc}")
    fb_path = base.get("forbidden_expressions_path")
    fb_payload = None
    if fb_path:
        full = (ROOT / fb_path).resolve()
        cfg.forbidden_source = fb_path
        try:
            fb_payload = _read_json(full)
        except FileNotFoundError:
            cfg.errors.append(f"질문별 금지 표현 파일 없음: {fb_path}")
        except (OSError, json.JSONDecodeError) as exc:
            cfg.errors.append(f"질문별 금지 표현 파일 오류: {fb_path}: {exc}")

    if override:
        if not isinstance(override, dict):
            raise ConfigError("AIONA 설정은 객체여야 합니다")
        if override.get("applies_to_system_ids"):
            cfg.applies_to_system_ids = tuple(str(x).lower() for x in override["applies_to_system_ids"])
        if "marker_registry" in override:
            registry = override["marker_registry"]
            cfg.marker_source = "업로드 설정 (marker_registry)"
            cfg.marker_status = str((registry or {}).get("status", ""))
        if "forbidden_expressions" in override:
            fb_payload = override["forbidden_expressions"]
            cfg.forbidden_source = "업로드 설정 (forbidden_expressions)"

    if registry is not None:
        cfg.marker_patterns = _marker_patterns(registry, groups, cfg.marker_source)
    if fb_payload is not None:
        cfg.forbidden_entries = _forbidden_entries(fb_payload, cfg.forbidden_source)
    cfg.raw_hash = stable_hash({"base": base, "registry": registry, "forbidden": fb_payload,
                                "systems": cfg.applies_to_system_ids})[:16]
    return cfg
