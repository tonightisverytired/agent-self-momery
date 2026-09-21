# -*- coding: utf-8 -*-
"""LLM 拆解模块。

Extractor 协议 + DeepSeek 参考实现（OpenAI 兼容，本地优先，凭据由调用层传入
或从 DEEPSEEK_API_KEY 环境变量读取）+ 确定性兜底实现。

校验层使用 Pydantic v2（PydanticAI 同源校验核心）：每条 LLM 候选独立校验，
残缺候选只丢弃自身，不再拖垮整批写入。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from typing import Optional, Protocol

from pydantic import (BaseModel, ConfigDict, Field,
                      ValidationError as PydanticValidationError,
                      field_validator, model_validator)

from .models import (BELIEF_POLARITIES, DEFAULT_RELATIONS,
                     EVIDENCE_SOURCE_TYPES, IMPACT_DIRECTIONS,
                     IMPACT_KINDS, IMPACT_VALENCES, INTENT_STATUSES,
                     TRIGGER_TYPES, ExtractedMemory, to_naive)

EVENT_KINDS = frozenset({
    "meeting", "chat", "work", "life", "health", "sport", "entertainment",
    "family", "food", "shopping", "travel", "decision", "deploy",
    "transient", "core", "other",
})
ENTITY_KINDS = frozenset({
    "person", "project", "place", "preference", "food", "pet", "media",
    "skill", "concept", "vendor", "other",
})

SYSTEM_PROMPT = """你是长期个人智能体的记忆抽取器。把用户提供的日常记录片段拆解为结构化记忆候选。
只抽取可验证的事实、事件、偏好、人物、项目等；不要编造；文本没提到时间就输出空字符串。
输出必须是合法 JSON，格式为 {"memories": [ ... ]}，不要输出任何解释文字。

字段说明：
- type=event: name(一句话摘要), kind(meeting/chat/work/life/health/sport/entertainment/family/food/shopping/travel/other), ts(ISO 日期时间或日期，可空字符串), value_score(0-1 对用户的重要度)
- type=entity: name(人物/项目/地点/事物名), kind(person/project/place/preference/food/pet/media/skill/concept/other), description(一句话描述)
- type=fact: name=实体名, key=属性名(如 favorite_drink), value=属性值, source(profile/chat/agent), confidence(0-1)
- type=edge: from_=起点实体名, to=终点实体名, rel(participates/discusses/mentions/prefers/depends_on/occurs_with/similar_to/part_of/precedes/causes/updates_to/contradicts/summarizes/step_of), confidence(0-1)
- type=belief: name=主体名(观点持有者，如"用户"), proposition=观点命题(如"上海生活成本高"), polarity(positive/negative/neutral), valid_at(观点生效时间，ISO 或空字符串), source, confidence(0-1)
- type=intent: name=主体名, proposition=意图内容(如"考虑离开上海"), status(active/completed/cancelled/expired/superseded), valid_at(ISO 或空字符串), confidence(0-1)
- type=evidence: source_type(user_statement/conversation/system_record/external_data/imported_memory/inferred), source_ref=来源引用(对话/消息标识), conversation_id(可选), message_id(可选), content_hash(可选), trust_level(1-5 数字)
- type=impact: subject=受影响主体名(如"用户"), dimension=影响维度(income/work/health/stress/relationship/time/learning/satisfaction/risk/cost/quality/convenience 或业务自定义维度), direction(increase/decrease/stable/appear/disappear), valence(positive/negative/neutral/mixed/unknown), magnitude(0-1 影响程度), kind(objective/subjective), evaluator(评价主体，如 user/agent), cause=引起该影响的事件名(可空字符串), description(一句话说明), valid_at(ISO 或空字符串), confidence(0-1)。direction 表示"发生了什么变化"，valence 表示"评价方向"，两者不得混为一谈（如 income increase 不必然 positive）。

完整 JSON 示例（含全部八种类型）：
{"memories": [
  {"type": "event", "name": "与张总讨论项目A预算", "kind": "meeting", "ts": "2026-08-03T10:30:00", "value_score": 0.7},
  {"type": "entity", "name": "张总", "kind": "person", "description": "项目负责人"},
  {"type": "fact", "name": "咖啡", "key": "favorite_drink", "value": "拿铁", "source": "chat", "confidence": 0.8},
  {"type": "edge", "from_": "张总", "to": "项目A", "rel": "participates", "confidence": 0.9},
  {"type": "belief", "name": "用户", "proposition": "上海生活成本高", "polarity": "negative", "valid_at": "2026-01-05", "source": "chat", "confidence": 0.8},
  {"type": "intent", "name": "用户", "proposition": "考虑离开上海", "status": "active", "valid_at": "2026-08-01", "confidence": 0.7},
  {"type": "evidence", "source_type": "conversation", "source_ref": "msg-2026-08-01-001", "conversation_id": "conv-42", "message_id": "m1", "trust_level": 5},
  {"type": "impact", "subject": "用户", "dimension": "income", "direction": "increase", "valence": "positive", "magnitude": 0.7, "kind": "objective", "evaluator": "user", "cause": "换工作", "description": "换工作后收入提高", "confidence": 0.8}
]}

硬性要求：
1. fact 必须同时给出 key 和 value；edge 必须同时给出 from_ 和 to，且 rel 只能是上面的枚举值。
2. belief/intent 必须给出 proposition；belief 的 polarity 只能是 positive/negative/neutral；intent 的 status 只能是 active/completed/cancelled/expired/superseded。
3. evidence 的 source_type 只能是 user_statement/conversation/system_record/external_data/imported_memory/inferred 之一。
4. impact 必须给出 subject/dimension/direction/valence；direction 只能是 increase/decrease/stable/appear/disappear；valence 只能是 positive/negative/neutral/mixed/unknown；kind 只能是 objective/subjective；magnitude 在 0-1 之间。
5. 时间规则：文本明确提到日期/时间才填 ts/valid_at；未提到时间必须输出空字符串 ""，禁止编造、禁止回填"今天"。
   "今天/明天/昨天/周末/下周一"等相对时间按用户提供的当前真实日期换算成具体日期后再输出。
6. 每条片段抽取 0-4 条候选，宁缺毋滥；但对话中若出现明确的属性/偏好/观点/计划信号（如"我喜欢…""我打算…""…太贵了"），应抽取对应 fact/belief/intent，不要只抽 event。
7. 实体名要与原文保持一致；同一批内先出现的实体可作为后续 fact/edge/belief/intent 的端点。
"""


def find_deepseek_key():
    """优先读取 DEEPSEEK_API_KEY，兼容旧的 DEEPSEEK* 前缀变量。"""
    exact = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if exact:
        return "DEEPSEEK_API_KEY", exact
    for name, value in os.environ.items():
        if name.startswith("DEEPSEEK") and name != "DEEPSEEK_MODEL" and value.strip():
            return name, value.strip()
    return None, None


def _parse_ts(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%Y/%m/%d"):
        try:
            # 带偏移量的输入（+08:00 / Z）必须归一到 naive：库内全链路用
            # naive，aware 混进去会让后续的时间比较与排序抛 TypeError
            return to_naive(datetime.strptime(text, fmt))
        except ValueError:
            continue
    try:                      # 兜底：fromisoformat 认 "Z" 等 Python 变体
        return to_naive(datetime.fromisoformat(text))
    except ValueError:
        return None


class MemoryCandidate(BaseModel):
    """LLM 候选的强校验模型（PydanticAI 同源 schema，可无缝用于 Agent 输出）。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)

    type: str
    name: Optional[str] = None
    kind: Optional[str] = None
    description: Optional[str] = None
    ts: Optional[str] = None
    value_score: Optional[float] = Field(default=None, ge=0, le=1)
    protected: bool = False
    source: str = "chat"
    confidence: float = Field(default=0.7, ge=0, le=1)
    rel: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    key: Optional[str] = None
    value: Optional[object] = None
    # 0.5.0 状态维度字段（belief/intent/evidence）
    proposition: Optional[str] = None
    polarity: Optional[str] = None
    status: Optional[str] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    valid_at: Optional[str] = None
    # 0.7.0 P1：impact 候选字段
    subject: Optional[str] = None
    dimension: Optional[str] = None
    direction: Optional[str] = None
    valence: Optional[str] = None
    magnitude: Optional[float] = Field(default=None, ge=0, le=1)
    impact_kind: Optional[str] = None
    evaluator: Optional[str] = None
    cause: Optional[str] = None
    # 0.7.0 P2：trigger 候选
    trigger_type: Optional[str] = None
    trigger_text: Optional[str] = None
    trigger_target_type: Optional[str] = None

    @field_validator("name", "kind", "rel", "from_", "to", "key",
                     "proposition", "source_ref", "conversation_id",
                     "message_id", "content_hash", mode="before")
    @classmethod
    def _str_or_none(cls, v):
        if v is None:
            return None
        return str(v).strip() or None

    @field_validator("value", mode="before")
    @classmethod
    def _value_str(cls, v):
        """事实值必须是「有内容的标量字符串」。

        空串/空容器/None 一律回 None，让 `_check_required` 判为缺 value 而
        拒绝该候选——原实现 `str(v).strip()` 会把 `""`/`[]`/`false` 变成
        可用值（`"[]"`、`"False"`）落库，空值事实随后参与裁决，能把真实值
        判失效（同源时以 valid_at 新者为胜）。
        """
        if v is None:
            return None
        if isinstance(v, (list, dict, tuple, set)):
            return None
        if isinstance(v, bool):
            return str(v).lower()
        text = str(v).strip()
        return text or None

    @model_validator(mode="after")
    def _check_required(self):
        t = self.type
        if t == "event":
            if not self.name:
                raise ValueError("event 缺少 name")
            if self.kind and self.kind not in EVENT_KINDS:
                self.kind = "other"
        elif t == "entity":
            if not self.name:
                raise ValueError("entity 缺少 name")
            if self.kind and self.kind not in ENTITY_KINDS:
                self.kind = "other"
        elif t == "fact":
            if not self.key:
                raise ValueError("fact 缺少 key")
            if self.value is None:
                raise ValueError("fact 缺少 value")
        elif t == "edge":
            if not self.from_ or not self.to:
                raise ValueError("edge 缺少 from_/to")
            if self.rel not in DEFAULT_RELATIONS:
                raise ValueError(f"edge 关系类型非法: {self.rel}")
        elif t == "belief":
            if not self.proposition:
                raise ValueError("belief 缺少 proposition")
            if self.polarity is not None \
                    and self.polarity not in BELIEF_POLARITIES:
                self.polarity = "neutral"
        elif t == "intent":
            if not self.proposition:
                raise ValueError("intent 缺少 proposition")
            if self.status is not None and self.status not in INTENT_STATUSES:
                self.status = "active"
        elif t == "evidence":
            if not self.source_type:
                raise ValueError("evidence 缺少 source_type")
            if self.source_type not in EVIDENCE_SOURCE_TYPES:
                raise ValueError(f"evidence source_type 非法: {self.source_type}")
        elif t == "impact":
            if not self.dimension:
                raise ValueError("impact 缺少 dimension")
            if not self.direction:
                raise ValueError("impact 缺少 direction")
            if self.direction not in IMPACT_DIRECTIONS:
                raise ValueError(f"impact direction 非法: {self.direction}")
            if not self.valence:
                raise ValueError("impact 缺少 valence")
            if self.valence not in IMPACT_VALENCES:
                raise ValueError(f"impact valence 非法: {self.valence}")
            # dimension 不校验枚举（文档 §7.1 允许业务扩展）
            # 提示词示例里主观/客观写在 `kind`（与 event/entity 同名字段），
            # 而模型字段叫 `impact_kind`：不兼容两处的话 LLM 输出永远落到
            # `kind`，`impact_kind` 恒 None → 所有影响一律被存成 objective
            if not self.impact_kind and self.kind:
                self.impact_kind = self.kind
            if self.impact_kind is not None \
                    and self.impact_kind not in IMPACT_KINDS:
                self.impact_kind = "objective"
        elif t == "trigger":
            if not self.name:
                raise ValueError("trigger 缺少 target 记忆名")
            if self.trigger_type not in TRIGGER_TYPES:
                raise ValueError(f"trigger_type 非法: {self.trigger_type}")
            if not self.trigger_text:
                raise ValueError("trigger 缺少 trigger_text")
        else:
            raise ValueError(f"未知候选类型: {t}")
        return self

    def to_extracted(self) -> ExtractedMemory:
        value = self.value
        if self.type == "entity":
            value = self.description or value
        if self.type in ("belief", "intent"):
            ts_text = self.valid_at or self.ts
            return ExtractedMemory(
                type=self.type,
                name=self.name or "",
                ts=_parse_ts(ts_text) if ts_text else None,
                source=self.source,
                confidence=self.confidence,
                proposition=self.proposition,
                polarity=self.polarity or "neutral",
                status=self.status or "active")
        if self.type == "evidence":
            return ExtractedMemory(
                type=self.type,
                source_type=self.source_type,
                source_ref=self.source_ref or "",
                conversation_id=self.conversation_id,
                message_id=self.message_id,
                content_hash=self.content_hash,
                trust_level=self.trust_level)
        if self.type == "impact":
            ts_text = self.valid_at or self.ts
            return ExtractedMemory(
                type=self.type,
                name=self.subject or "",
                ts=_parse_ts(ts_text) if ts_text else None,
                source=self.source,
                confidence=self.confidence,
                dimension=self.dimension,
                direction=self.direction,
                valence=self.valence,
                magnitude=self.magnitude,
                impact_kind=self.impact_kind or "objective",
                evaluator=self.evaluator or "agent",
                description=self.description or "",
                cause=self.cause)
        if self.type == "trigger":
            return ExtractedMemory(
                type=self.type,
                name=self.name or "",
                confidence=self.confidence,
                trigger_type=self.trigger_type,
                trigger_text=self.trigger_text,
                trigger_target_type=self.trigger_target_type
                or self.kind or "event")
        return ExtractedMemory(
            type=self.type,
            name=self.name or "",
            kind=self.kind,
            ts=_parse_ts(self.ts) if self.ts else None,
            value_score=self.value_score,
            protected=self.protected,
            source=self.source,
            confidence=self.confidence,
            rel=self.rel,
            from_=self.from_,
            to=self.to,
            key=self.key,
            value=value,
        )


class Extractor(Protocol):
    def extract(self, text: str, meta: dict | None = None) -> list[ExtractedMemory]: ...

    def extract_many(self, texts: list[str], meta: dict | None = None,
                     batch_size: int = 20) -> list[list[ExtractedMemory]]: ...


class CausalProposer(Protocol):
    """LLM 因果提议（0.6.0 P2，可选）：只提议，不自动落库。

    返回 [(proposition, confidence), ...]；调用方显式落 memory_links。
    """

    def propose(self, event, fact_change) -> list[tuple[str, float]]: ...


class DeepSeekCausalProposer:
    """DeepSeek 因果提议参考实现（惰性：构造不依赖网络，仅 propose 时调用）。"""

    def __init__(self, api_key=None, model=None,
                 base_url="https://api.deepseek.com",
                 timeout=120, max_tokens=1500):
        self.api_key = api_key or (find_deepseek_key() or (None, None))[1]
        self.model = model or os.environ.get("DEEPSEEK_MODEL") \
            or "deepseek-v4-pro"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def propose(self, event, fact_change):
        """LLM 因果提议；失败返回空列表（弱 LLM 原则：规则推导为主）。"""
        import requests
        prompt = (
            "以下是一个记忆状态变化：\n"
            f"事件：{event}\n变化：{fact_change}\n"
            "请给出 1-3 条可能的因果关系说明，输出 JSON："
            '{"causes": [{"proposition": "...", "confidence": 0.8}]}'
            "；不确定就输出空数组。")
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model,
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0.2,
                      "max_tokens": self.max_tokens},
                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()["choices"][0]["message"]["content"]
            causes = json.loads(data).get("causes", [])
            return [(str(c.get("proposition", "")),
                     float(c.get("confidence", 0.5))) for c in causes
                    if c.get("proposition")]
        except Exception:  # noqa: BLE001 LLM 不可用 → 空提议
            return []


class DeepSeekExtractor:
    """DeepSeek（OpenAI 兼容）参考实现。

    参数与官方文档对齐：
    - base_url: https://api.deepseek.com（OpenAI SDK 直接使用，无需 /v1）
    - model: deepseek-v4-pro / deepseek-v4-flash（官方当前模型名，deepseek-chat 仍兼容）
    - response_format={"type":"json_object"} + prompt 含 json 字样与示例
    - max_tokens 显式设置，防止 JSON 中途截断；timeout 显式设置，避免挂起
    """

    def __init__(self, api_key: str | None = None,
                 model: str | None = None,
                 base_url: str = "https://api.deepseek.com",
                 timeout: int = 120, max_retries: int = 2,
                 max_tokens: int = 3000,
                 today: str | None = None,
                 timezone: str = "Asia/Shanghai",
                 thinking: str = "disabled"):
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.timezone = timezone
        self.today = today
        self.thinking = thinking
        self.last_rejects: list[tuple[str, str]] = []
        if not api_key:
            env_name, api_key = find_deepseek_key()
            if not api_key:
                raise ValueError("未找到 DEEPSEEK_API_KEY 环境变量中的 API key")
        # requests 惰性导入：构造/解析不依赖网络库，仅真实 API 调用时需要。
        self._requests = None
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _http(self):
        if self._requests is None:
            try:
                import requests
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "需要 requests 包：pip install requests") from e
            self._requests = requests
        return self._requests

    def _context_line(self, meta: dict | None = None) -> str:
        meta = meta or {}
        today = meta.get("today") or self.today
        tz = meta.get("timezone") or self.timezone
        if today:
            return (f"当前真实日期：{today}（时区 {tz}）。"
                    "文本中的“今天/明天/昨天/周末/下周”等相对时间必须换算成具体日期填入 ts；"
                    "文本没有提到时间则 ts 必须为空字符串。")
        return (f"时区：{tz}。文本没有提到时间时 ts 必须为空字符串，禁止编造时间。")

    def _call(self, user_prompt: str) -> dict:
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": self.max_tokens,
                }
                # v4 系列默认开启 thinking，推理会先消耗 max_tokens，
                # 导致 content 为空、finish_reason=length（官方文档明确该机制）。
                # 抽取任务只需结构化 JSON，显式关闭 thinking。
                if self.model.startswith("deepseek-v4"):
                    payload["thinking"] = {"type": self.thinking}
                resp = self._http().post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                choice = payload["choices"][0]
                content = choice["message"]["content"]
                if not content:
                    raise RuntimeError(
                        "DeepSeek 返回空内容（JSON 模式偶发），按官方文档调整提示词后重试")
                if choice.get("finish_reason") == "length":
                    raise RuntimeError("max_tokens 截断 JSON，增大 max_tokens 后重试")
                return json.loads(content)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"DeepSeek 调用失败: {last_err}")

    def _parse(self, data: dict) -> tuple[list[ExtractedMemory],
                                          list[tuple[str, str]]]:
        if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
            raise RuntimeError("LLM 输出缺少 memories 数组，无法解析")
        out: list[ExtractedMemory] = []
        rejects: list[tuple[str, str]] = []
        for item in data["memories"]:
            if not isinstance(item, dict):
                rejects.append(("?", "候选不是 JSON 对象"))
                continue
            try:
                cand = MemoryCandidate.model_validate(item)
                out.append(cand.to_extracted())
            except PydanticValidationError as e:
                first = e.errors()[0] if e.errors() else {}
                loc = ".".join(str(x) for x in first.get("loc", ()))
                msg = first.get("msg", str(e))
                rejects.append((str(item.get("type", "?")),
                                f"{loc}: {msg}" if loc else msg))
            except Exception as e:  # noqa: BLE001
                rejects.append((str(item.get("type", "?")), str(e)))
        return out, rejects

    @staticmethod
    def _tag(cands: list[ExtractedMemory], text: str):
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        for i, c in enumerate(cands):
            c.idempotency_key = f"llm:{h}:{i}"

    def extract(self, text: str, meta: dict | None = None) -> list[ExtractedMemory]:
        prompt = (
            f"请抽取以下日常记录（1 条）：\n1. {text}\n"
            f"{self._context_line(meta)}\n"
            '输出 JSON：{"memories":[...]}')
        cands, rejects = self._parse(self._call(prompt))
        self.last_rejects = rejects
        self._tag(cands, text)
        return cands

    def extract_many(self, texts, meta=None, batch_size=20):
        results = []
        all_rejects = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            numbered = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(chunk))
            prompt = (
                f"请抽取以下日常记录（共 {len(chunk)} 条，编号 1-{len(chunk)}）：\n"
                f"{numbered}\n{self._context_line(meta)}\n"
                '输出 JSON：{"memories":[...]}，候选不要包含编号字段。')
            cands, rejects = self._parse(self._call(prompt))
            all_rejects.extend(rejects)
            self._tag(cands, f"batch:{i}:{'|'.join(t[:20] for t in chunk)}")
            results.append(cands)
        self.last_rejects = all_rejects
        return results


class FallbackExtractor:
    """确定性兜底：每条片段生成一个低价值事件候选（无 LLM 也可导入）。"""

    def extract(self, text, meta=None):
        return [ExtractedMemory(type="event", name=text[:80], kind="chat",
                                value_score=0.3)]

    def extract_many(self, texts, meta=None, batch_size=20):
        return [self.extract(t) for t in texts]
