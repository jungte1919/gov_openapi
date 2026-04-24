from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .api_keys import resolve_api_key


OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.3"
DEFAULT_TIMEOUT = 180
DEFAULT_INSTRUCTIONS = (
    "당신은 한국 사회정책과 사회통계 보고서를 작성하는 선임 정책분석관이다. "
    "항상 학술형 문어체로 작성하고, 정책담당자와 연구자가 검토해도 무리가 없을 정도의 정확성과 엄밀성을 유지하라. "
    "문단 수를 임의로 제한하지 말고, 충분한 정보가 전달될 정도로 서술하라. "
    "기존 해설이 있으면 그 핵심 논지와 구조를 최대한 유지하되, 부정확하거나 낡은 부분은 최신 맥락에 맞게 정교하게 수정하라. "
    "기존 해설이 없으면 새로 작성하라. "
    "과장, 감탄, 구어체, 선동적 표현을 피하고, 정책 수단, 제도 유형, 대상 집단, 재정 구조를 명확히 구분하라. "
    "데이터나 맥락만으로 단정하기 어려운 해석은 한계를 분명히 적시하라. "
    "출력은 JSON 스키마를 엄격히 따르라."
)


@dataclass(frozen=True, slots=True)
class PolicyNoteConfig:
    model: str = DEFAULT_MODEL
    timeout_seconds: int = DEFAULT_TIMEOUT
    key_name: str = "OPENAI"
    key_filenames: tuple[str, ...] = ("openai_api_key.txt", "opepai_api_key.txt")
    instructions: str = DEFAULT_INSTRUCTIONS
    schema_name: str = "policy_section_answer"


def resolve_openai_api_key(config: PolicyNoteConfig | None = None) -> str | None:
    active = config or PolicyNoteConfig()
    for filename in active.key_filenames:
        key = resolve_api_key(key_name=active.key_name, default_filename=filename)
        if key:
            return key
    return None


def strip_existing_ai_blocks(markdown: str) -> str:
    return re.sub(
        r"<!-- AI_ANSWER:[^>]+:start -->[\s\S]*?<!-- AI_ANSWER:[^>]+:end -->",
        "",
        markdown or "",
        flags=re.MULTILINE,
    ).strip()


def extract_output_text(response_json: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if isinstance(response_json.get("output_text"), str):
        return str(response_json["output_text"])
    for item in response_json.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()


def _policy_note_schema(name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "change_summary": {"type": "string"},
            },
            "required": ["title", "body", "change_summary"],
            "additionalProperties": False,
        },
    }


def build_policy_note_request(
    payload: Mapping[str, Any], config: PolicyNoteConfig | None = None
) -> dict[str, Any]:
    active = config or PolicyNoteConfig()
    clean_markdown = strip_existing_ai_blocks(str(payload.get("sectionMarkdown", "")))
    visual_context = payload.get("visualContext") or []
    extra_instruction = str(payload.get("instructionHint", "")).strip()
    instructions = active.instructions
    if extra_instruction:
        instructions = f"{instructions} 추가 지시: {extra_instruction}"
    return {
        "model": active.model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"보고서 제목: {payload.get('reportTitle', '')}"},
                    {"type": "input_text", "text": f"장 제목: {payload.get('chapterTitle', '')}"},
                    {"type": "input_text", "text": f"절 제목: {payload.get('sectionTitle', '')}"},
                    {"type": "input_text", "text": f"질문: {payload.get('question', '')}"},
                    {"type": "input_text", "text": f"절 본문 맥락: {clean_markdown or '없음'}"},
                    {
                        "type": "input_text",
                        "text": f"도표 맥락: {json.dumps(visual_context, ensure_ascii=False)}",
                    },
                    {
                        "type": "input_text",
                        "text": "기존 해설: " + (str(payload.get("existingAnswer", "")).strip() or "없음"),
                    },
                    {
                        "type": "input_text",
                        "text": (
                            "작성 지시: 질문에 대한 정책 해설 본문을 작성하라. "
                            "기본은 연속 문단형 평문으로 쓰고, 필요한 경우에만 짧은 문장으로 보충하라. "
                            "정책의 제도 유형, 대상, 집행 방식, 최근 쟁점, 해석상 한계를 균형 있게 설명하라."
                        ),
                    },
                ],
            }
        ],
        "text": {"format": _policy_note_schema(active.schema_name)},
    }


def generate_policy_note(
    payload: Mapping[str, Any], config: PolicyNoteConfig | None = None
) -> dict[str, str]:
    active = config or PolicyNoteConfig()
    api_key = resolve_openai_api_key(active)
    if not api_key:
        filenames = ", ".join(active.key_filenames)
        raise RuntimeError(
            f"OpenAI API 키를 찾지 못했습니다. apifunction 폴더의 {filenames} 파일을 확인해 주세요."
        )

    request_body = build_policy_note_request(payload, active)
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=active.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API 호출에 실패했습니다. HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI API 연결에 실패했습니다: {exc.reason}") from exc

    response_json = json.loads(raw)
    output_text = extract_output_text(response_json)
    if not output_text:
        raise RuntimeError("OpenAI 응답 본문을 읽지 못했습니다.")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI 응답 JSON 파싱에 실패했습니다: {output_text}") from exc
    return {
        "title": str(parsed.get("title", payload.get("question", ""))).strip(),
        "body": str(parsed.get("body", "")).strip(),
        "change_summary": str(parsed.get("change_summary", "")).strip() or "AI 해설을 갱신했습니다.",
    }


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "DEFAULT_MODEL",
    "PolicyNoteConfig",
    "build_policy_note_request",
    "extract_output_text",
    "generate_policy_note",
    "resolve_openai_api_key",
    "strip_existing_ai_blocks",
]
