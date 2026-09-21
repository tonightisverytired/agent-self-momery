# -*- coding: utf-8 -*-
"""数据模型与配置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

from .errors import ValidationError

DEFAULT_RELATIONS = frozenset({
    "participates", "discusses", "mentions", "occurs_with", "precedes",
    "causes", "depends_on", "similar_to", "part_of", "prefers",
    "updates_to", "contradicts", "summarizes", "step_of",
})

BELIEF_POLARITIES = frozenset({"positive", "negative", "neutral"})
INTENT_STATUSES = frozenset({
    "active", "completed", "cancelled", "expired", "superseded"})
EVIDENCE_SOURCE_TYPES = frozenset({
    "user_statement", "conversation", "system_record", "external_data",
    "imported_memory", "inferred"})
MEMORY_LINK_RELATIONS = frozenset({
    "before", "after", "overlaps", "changes", "reinforces",
    "contradicts", "supersedes", "caused_by", "summarizes"})
SCORE_WEIGHT_KEYS = ("retrieval", "temporal", "validity", "source",
                     "evidence", "coherence", "conflict_penalty")

# 0.7.0 P1：Impact Memory（文档 §6/§7）——Direction 与 Valence 不得混为一谈
IMPACT_DIRECTIONS = frozenset({
    "increase", "decrease", "stable", "appear", "disappear"})
IMPACT_VALENCES = frozenset({
    "positive", "negative", "neutral", "mixed", "unknown"})
IMPACT_KINDS = frozenset({"objective", "subjective"})
# 推荐通用维度（校验不强制枚举，§7.1 允许业务扩展）
DEFAULT_IMPACT_DIMENSIONS = (
    "income", "work", "health", "stress", "relationship", "time",
    "learning", "satisfaction", "risk", "cost", "quality", "convenience")

# 0.7.0 P2：Associative Memory 触达路径（文档 §9）。
# Trigger 是「找到」的路径，不是证据，也不是最终事实。
TRIGGER_TYPES = frozenset({"entity", "bridge", "scene", "horizon"})


def to_naive(value):
    """把时间归一到「本地时区的 naive datetime」；None/空/非法值返回 None。

    库内全链路用 naive（`datetime.now()`），而 ISO 字符串允许带时区偏移
    （`2026-08-03T10:30:00+08:00`）。一旦 aware 混进链路，`aware > naive`
    与 `aware - naive` 都会抛 TypeError（且异常发生在排序/窗口计算等深处）。
    读边界（store._dt）与输入解析（extract._parse_ts / CLI）统一在这里收口。
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value

# 0.7.0 P3：个人模式类型（文档 §17）。Pattern 是「总结/推断」，
# 必须显式标记 source="inferred"，不得伪装成原始事实。
PATTERN_TYPES = frozenset({
    "preference", "behavior", "belief", "decision", "impact", "temporal"})

# 0.7.0 S0：同义 key 族（确定性归一化，canonical → 别名集）。
# 默认族刻意最小（仅位置族），避免触碰既有「口味/偏好」冲突语料分组语义。
DEFAULT_KEY_ALIASES = {
    "位置": frozenset({"位置", "location", "city", "address", "居住地",
                       "住址", "所在地", "居住城市"}),
}
# 0.8.2：查询侧族成员（仅 _relevance 匹配用，不进裁决分组——
# 裁决分组用 key_aliases 只并真同义 key；把 Title/Occupation、
# Achievements/Awards 这类**不同属性**并入同一 canonical 会让同节点
# 两个事实互踢出 current_state）
DEFAULT_KEY_QUERY_ALIASES = {
    "位置": frozenset({"位置", "location", "city", "address", "居住地",
                       "住址", "所在地", "居住城市"}),
    "职业": frozenset({"职业", "工作", "职位", "头衔", "occupation",
                       "profession", "job", "Occupation", "Title"}),
    "雇主": frozenset({"雇主", "单位", "公司", "任职机构", "employer",
                       "Employer"}),
    "教育": frozenset({"教育", "教育背景", "学历", "专业", "毕业院校",
                       "education", "Education Background", "major",
                       "research_field", "研究方向"}),
    "奖项": frozenset({"奖项", "获奖", "成就", "荣誉", "awards", "Awards",
                       "Achievements", "Awards and Role Models"}),
    "榜样": frozenset({"榜样", "偶像", "Role Models", "role_model"}),
    "爱好": frozenset({"爱好", "兴趣", "兴趣偏好", "hobby", "Hobbies",
                       "interest"}),
    "年龄": frozenset({"年龄", "age", "Age"}),
    "姓名": frozenset({"姓名", "名字", "昵称", "称呼", "name", "Nickname"}),
    "国籍": frozenset({"国籍", "nationality", "Nationality"}),
    "民族": frozenset({"民族", "ethnicity", "Ethnic Background"}),
    "性别": frozenset({"性别", "gender", "Gender"}),
    "外貌": frozenset({"外貌", "外形", "长相", "Physical Characteristics"}),
    # 「关系_李明」这类 族_实例 key 由前缀规则覆盖（仅查询侧）
    "关系": frozenset({"关系", "relationship", "relation"}),
}
# 各族的中文查询词（current_state 相关性排序用）
DEFAULT_KEY_QUERY_WORDS = {
    "位置": frozenset({"住", "住哪", "住在", "居住", "城市", "地址", "在哪",
                       "搬", "住址"}),
    "职业": frozenset({"职业", "工作", "职位", "头衔", "做什么工作"}),
    "雇主": frozenset({"雇主", "单位", "公司", "任职", "就职", "哪家机构"}),
    "教育": frozenset({"教育", "学历", "专业", "毕业", "学校", "学的是",
                       "研究方向", "读的什么"}),
    "奖项": frozenset({"奖", "奖项", "获奖", "成就", "荣誉", "得过什么"}),
    "榜样": frozenset({"榜样", "偶像"}),
    "爱好": frozenset({"爱好", "兴趣", "喜欢什么", "喜欢做什么"}),
    "年龄": frozenset({"年龄", "几岁", "多大", "年纪"}),
    "姓名": frozenset({"名字", "姓名", "昵称", "称呼", "叫什么"}),
    "国籍": frozenset({"国籍", "哪国人"}),
    "民族": frozenset({"民族"}),
    "性别": frozenset({"性别"}),
    "外貌": frozenset({"外貌", "长相", "长什么样", "外形"}),
    "关系": frozenset({"关系", "认识", "是什么关系"}),
}


@dataclass(frozen=True)
class LifecycleDefaults:
    life: float
    decay_rate: float


@dataclass(frozen=True)
class MemoryConfig:
    """全部可变配置集中在调用层（构造级）。"""

    relations: frozenset = DEFAULT_RELATIONS
    source_rank: Mapping[str, int] = field(
        default_factory=lambda: {
            # 设计稿 §4 业务级来源可信级（五档）+ 0.3.0 历史别名兼容：
            # profile 即用户陈述；agent 记录按导入记忆；chat 提取低于两者。
            "system_record": 5, "external_data": 5,
            "user_statement": 4, "profile": 4,
            "imported_memory": 3, "agent": 3,
            "chat": 2, "inferred": 1})
    event_defaults: Mapping[str, LifecycleDefaults] = field(
        default_factory=lambda: {
            "transient": LifecycleDefaults(life=50.0, decay_rate=0.28),
            "core": LifecycleDefaults(life=150.0, decay_rate=0.02),
        })
    rrf_k: float = 60.0
    access_boost: tuple = (3.0, 6.0)
    conflict_confirm_threshold: float = 0.8
    lifecycle_order: tuple = ("active", "archived", "tombstoned", "deleted")
    max_hops_default: int = 2
    time_tolerance_days_default: int = 2
    dense_min_sim: float = 0.55
    # 0.8.1 二轮：分类型稠密阈值能力（默认关闭）。实测 PerLTQA 上
    # chat 降阈到 0.42 会让转写节点挤占事件 top-10（总分 -1.0），
    # 仅建议在以长文对话为主的库上显式开启，如 {"chat": 0.48}
    dense_min_sim_by_kind: Mapping[str, float] = field(
        default_factory=dict)
    sparse_min_sim: float = 0.25
    # 词面路候选准入阈值：作用于 bigram 包含度（不乘 value_score，
    # 价值分只参与排序）
    lexical_min_score: float = 0.15
    # 0.8.1：查询文本时间感知（显式日期/今天/昨天/上周等激活时间路）
    time_aware_text: bool = True
    dense_ann_top_k: int = 1000
    evidence_min_trust: float = 1.0
    # 0.7.0 S0：同义 key 族（canonical → 别名集，业务可覆盖扩充）
    key_aliases: Mapping[str, frozenset] = field(
        default_factory=lambda: dict(DEFAULT_KEY_ALIASES))
    key_query_words: Mapping[str, frozenset] = field(
        default_factory=lambda: dict(DEFAULT_KEY_QUERY_WORDS))
    # 0.8.2：查询侧族成员（仅 _relevance 族匹配，不参与裁决分组）
    key_query_aliases: Mapping[str, frozenset] = field(
        default_factory=lambda: dict(DEFAULT_KEY_QUERY_ALIASES))
    # 0.7.0 P2：trigger 非精确匹配的 bigram 重叠率阈值（单字只精确）
    trigger_match_threshold: float = 0.8
    # 0.7.0 P3：模式提取参数
    pattern_min_confidence: float = 0.5
    pattern_window_days: int = 28
    score_weights: Mapping[str, float] = field(
        default_factory=lambda: dict.fromkeys(SCORE_WEIGHT_KEYS, 1.0))

    def __post_init__(self):
        if not self.relations:
            raise ValidationError("E001 relations 不能为空")
        if "profile" not in self.source_rank:
            raise ValidationError("E001 source_rank 必须包含 profile")
        if not (0 <= self.conflict_confirm_threshold <= 1):
            raise ValidationError("E002 阈值必须在 [0,1]")
        if self.rrf_k <= 0:
            raise ValidationError("E001 rrf_k 必须 > 0")
        if not (0 <= self.dense_min_sim <= 1):
            raise ValidationError("E002 dense_min_sim 必须在 [0,1]")
        if not (0 <= self.sparse_min_sim <= 1):
            raise ValidationError("E002 sparse_min_sim 必须在 [0,1]")
        if not (0 <= self.lexical_min_score <= 1):
            raise ValidationError("E002 lexical_min_score 必须在 [0,1]")
        if self.dense_ann_top_k <= 0:
            raise ValidationError("E001 dense_ann_top_k 必须 > 0")
        if not (0 <= self.evidence_min_trust <= 5):
            raise ValidationError("E002 evidence_min_trust 必须在 [0,5]")
        if not (0 <= self.trigger_match_threshold <= 1):
            raise ValidationError(
                "E002 trigger_match_threshold 必须在 [0,1]")
        if not (0 <= self.pattern_min_confidence <= 1):
            raise ValidationError(
                "E002 pattern_min_confidence 必须在 [0,1]")
        if self.pattern_window_days <= 0:
            raise ValidationError("E001 pattern_window_days 必须 > 0")
        for st in ("active", "archived", "tombstoned", "deleted"):
            if st not in self.lifecycle_order:
                raise ValidationError(f"E001 lifecycle_order 缺少 {st}")
        missing = set(SCORE_WEIGHT_KEYS) - set(self.score_weights)
        if missing:
            raise ValidationError(
                f"E001 score_weights 缺少键: {sorted(missing)}")
        for k, v in self.score_weights.items():
            if v < 0:
                raise ValidationError(f"E002 score_weights[{k}] 必须 ≥ 0")


@dataclass
class Node:
    nid: int
    node_type: str
    kind: str
    name: str
    description: str
    ts: Optional[datetime]
    value_score: float
    protected: bool
    access_label: str
    lifecycle: str
    life: float
    decay_rate: float
    last_access: Optional[datetime]
    created_at: datetime
    evidence_ids: list = field(default_factory=list)
    source: str = ""


@dataclass
class Edge:
    eid: int
    from_id: int
    to_id: int
    rel: str
    weight: float
    confidence: float
    valid_at: datetime
    invalid_at: Optional[datetime]
    created_at: datetime
    access_count: int
    lifecycle: str


@dataclass
class Fact:
    fid: int
    node_id: int
    key: str
    value: str
    source: str
    confidence: float
    valid_at: datetime
    recorded_at: datetime
    invalid_at: Optional[datetime]
    superseded_by: Optional[int]
    tombstoned: bool
    evidence_ids: list = field(default_factory=list)
    explicit_confirmation: bool = False


@dataclass
class ConflictGroup:
    """同 (node_id, canonical_key) 上的并列冲突（查询端冲突态，0.8.0）。

    FactResolver 判定「同源同高置信」时给出的是**一组并列 contender**，
    组结构必须一路带到序列化层：此前它被拍平成 Fact 列表，下游只能按
    原始 key 重新分组，导致同义 key（位置/所在地/location）被劈成多条、
    扁平截断把一个组腰斩成单值条目（外观上像事实自冲突）。
    """

    node_id: int
    key: str              # 展示用 key：取组内第一条的原始 key
    canonical_key: str    # 判定用的归一化 key
    fact_ids: list = field(default_factory=list)
    values: list = field(default_factory=list)
    kind: str = "fact_conflict"

    def __len__(self):
        return len(self.fact_ids)


@dataclass
class Tombstone:
    tid: int
    target_type: str
    target_id: int
    reason: str
    at: datetime


@dataclass
class Belief:
    """主体对命题的主观认知（0.5.0）；历史变化不覆盖，演化链走 memory_links。"""

    id: int
    subject_id: int
    proposition: str
    polarity: str = "neutral"
    confidence: float = 0.7
    source: str = "chat"
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    superseded_by: Optional[int] = None
    lifecycle: str = "active"
    access_label: str = "public"
    evidence_ids: list = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.polarity not in BELIEF_POLARITIES:
            raise ValidationError(
                f"E001 非法 polarity: {self.polarity}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class Intent:
    """未来倾向/计划；status 独立于 lifecycle，过期计划不当现在意图。"""

    id: int
    subject_id: int
    proposition: str
    status: str = "active"
    confidence: float = 0.7
    source: str = "chat"
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    lifecycle: str = "active"
    access_label: str = "public"
    evidence_ids: list = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.status not in INTENT_STATUSES:
            raise ValidationError(f"E001 非法 status: {self.status}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class Evidence:
    """记忆的证据来源；inferred 必须标注推断，不得伪装为客观事实。"""

    id: int
    source_type: str
    source_ref: str = ""
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    observed_at: Optional[datetime] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    created_at: Optional[datetime] = None
    access_label: str = "public"

    def __post_init__(self):
        if self.source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValidationError(
                f"E001 非法 source_type: {self.source_type}")


@dataclass
class Impact:
    """事件/行为/决策对主体的影响（0.7.0，文档 §6）。

    Direction（发生了什么变化）与 Valence（评价方向）分列；
    objective/subjective 区分 + evaluator 显式保存（§7.2）。
    """

    id: int
    subject_id: int
    dimension: str
    direction: str
    valence: str
    magnitude: float = 0.5
    kind: str = "objective"
    evaluator: str = "agent"
    description: str = ""
    cause_event_id: Optional[int] = None
    source: str = "chat"
    confidence: float = 0.7
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    superseded_by: Optional[int] = None
    lifecycle: str = "active"
    access_label: str = "public"
    evidence_ids: list = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.direction not in IMPACT_DIRECTIONS:
            raise ValidationError(
                f"E001 非法 direction: {self.direction}")
        if self.valence not in IMPACT_VALENCES:
            raise ValidationError(f"E001 非法 valence: {self.valence}")
        if self.kind not in IMPACT_KINDS:
            raise ValidationError(f"E001 非法 kind: {self.kind}")
        if not (0.0 <= self.magnitude <= 1.0):
            raise ValidationError("E002 magnitude 必须在 [0,1]")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class Trigger:
    """记忆的触达路径（0.7.0 P2，文档 §9）。

    Entity/Bridge/Scene/Horizon 四类；memory_id+memory_type 跨表定位
    目标记忆；source=rule（确定性）或 llm（仅提议，落库仍走校验）。
    """

    id: int
    memory_id: int
    memory_type: str
    trigger_type: str
    text: str
    source: str = "rule"
    confidence: float = 0.7
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.trigger_type not in TRIGGER_TYPES:
            raise ValidationError(
                f"E001 非法 trigger_type: {self.trigger_type}")
        if self.memory_type not in ("event", "fact", "belief", "intent",
                                    "impact"):
            raise ValidationError(
                f"E001 非法 memory_type: {self.memory_type}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")


@dataclass
class Pattern:
    """个人长期状态模式（0.7.0 P3，文档 §17）。

    Preference/Behavior/Belief/Decision/Impact/Temporal 六类；
    support=支撑样本数；window_days=统计窗口；source 强制 "inferred"
    （总结/推断不得伪装原始事实，§17 硬性要求）。
    """

    id: int
    pattern_type: str
    subject_id: int
    proposition: str
    confidence: float = 0.7
    support: int = 1
    window_days: Optional[int] = None
    source: str = "inferred"
    evidence_ids: list = field(default_factory=list)
    valid_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.pattern_type not in PATTERN_TYPES:
            raise ValidationError(
                f"E001 非法 pattern_type: {self.pattern_type}")
        if self.source != "inferred":
            self.source = "inferred"
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.support < 0:
            raise ValidationError("E002 support 必须 ≥ 0")


@dataclass
class MemoryLink:
    """高阶记忆逻辑关系层（与实体邻接 edges 分层）。

    source_type 标记 target 维度（TemporalChainBuilder 消费约定）；
    source_dim（0.7.0 起）显式标记 source 维度，跨表 id 碰撞时消歧。
    """

    id: int
    source_id: int
    target_id: int
    source_type: str
    relation: str
    confidence: float = 0.7
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    source_dim: str = ""

    def __post_init__(self):
        if self.relation not in MEMORY_LINK_RELATIONS:
            raise ValidationError(f"E001 非法 relation: {self.relation}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class MemoryState:
    """Resolver 输出的当前业务状态（0.5.0）。"""

    current_facts: list = field(default_factory=list)
    historical_facts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    beliefs: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    chains: list = field(default_factory=list)
    # 0.7.0 P1/P3
    impacts: list = field(default_factory=list)
    impact_history: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    # 0.8.0：局部解析失败记录 [{"dimension": "fact", "error": "..."}]
    # 原实现是 `except Exception: state.X = []`——维度被清空却无任何信号，
    # 上层只看到「库里没事实」。这里把失败显式记下来，由 recall_context
    # 写进 notes 与 trace。
    degraded: list = field(default_factory=list)


@dataclass
class ResolvedMemory:
    """经 Resolver 处理后的记忆封装（维度标记）。"""

    memory: object = None
    dimension: str = ""


@dataclass
class MemoryScore:
    """状态层分项评分（仅 recall_context 管线使用，§23 可解释）。"""

    retrieval_score: float = 0.0
    temporal_score: float = 0.0
    validity_score: float = 0.0
    source_score: float = 0.0
    evidence_score: float = 0.0
    coherence_score: float = 0.0
    conflict_penalty: float = 0.0

    def final(self, weights: Optional[Mapping[str, float]] = None) -> float:
        """final = α·retrieval + … + ζ·coherence − η·conflict_penalty。

        默认权重全 1.0（等权求和）。
        """
        w = weights or dict.fromkeys(SCORE_WEIGHT_KEYS, 1.0)
        return (w["retrieval"] * self.retrieval_score
                + w["temporal"] * self.temporal_score
                + w["validity"] * self.validity_score
                + w["source"] * self.source_score
                + w["evidence"] * self.evidence_score
                + w["coherence"] * self.coherence_score
                - w["conflict_penalty"] * self.conflict_penalty)


@dataclass
class CoherenceResult:
    """多维一致性检查结果。"""

    consistent: bool = True
    conflicts: list = field(default_factory=list)
    explanations: list = field(default_factory=list)


@dataclass
class RecallTrace:
    """recall_context 管线可观测性（设计稿 §26）。

    每次回答可定位「没召回/解析错/链断/证据不足/表达错」。
    仅内存对象，不持久化（§26「目的不是增加日志」）。
    """

    query: str = ""
    query_type: str = "semantic_recall"
    retrieval_mode: str = "triple"
    candidate_count: int = 0
    rrf_candidates: int = 0
    resolved_count: int = 0
    current_state_count: int = 0
    history_count: int = 0
    belief_count: int = 0
    chain_count: int = 0
    conflict_count: int = 0
    evidence_count: int = 0
    final_context_count: int = 0
    # 0.7.0 新维度计数
    impact_count: int = 0
    impact_chain_count: int = 0
    trigger_count: int = 0
    pattern_count: int = 0
    # 0.8.0：解析失败的维度数（>0 时 notes 里会有对应提示）
    degraded_count: int = 0
    chain_node_count: int = 0
    llm_used: bool = False
    fallback_used: bool = False

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


@dataclass(frozen=True)
class RelationFilter:
    node: str
    rel_type: Optional[str] = None


@dataclass(frozen=True)
class RecallQuery:
    text: Optional[str] = None
    time: Optional[tuple] = None          # (datetime, tol_days)
    topic: Optional[list] = None
    relation: Optional[RelationFilter] = None


@dataclass(frozen=True)
class RecallFilters:
    node_types: Optional[tuple] = None
    kinds: Optional[tuple] = None
    time_range: Optional[tuple] = None
    include_archived: bool = False
    access_labels: Optional[tuple] = None   # None = public+private，sensitive 隐藏


@dataclass(frozen=True)
class MemoryHit:
    node_id: int
    node_type: str
    name: str
    ts: Optional[datetime]
    score: float
    sources: tuple
    # 各召回路径的原始分与路内排名：{path: {"score": float, "rank": int}}
    path_scores: Optional[dict] = None


@dataclass
class ExtractedMemory:
    type: str
    name: str = ""
    kind: Optional[str] = None
    ts: Optional[datetime] = None
    value_score: Optional[float] = None
    protected: bool = False
    source: str = "chat"
    confidence: float = 0.7
    rel: Optional[str] = None
    from_: Optional[str] = None
    to: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    idempotency_key: Optional[str] = None
    # 0.5.0 状态维度候选字段（belief/intent/evidence）
    proposition: Optional[str] = None
    polarity: Optional[str] = None
    status: Optional[str] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    evidence_ids: Optional[list] = None
    # 0.7.0 P1：impact 候选字段
    dimension: Optional[str] = None
    direction: Optional[str] = None
    valence: Optional[str] = None
    magnitude: Optional[float] = None
    impact_kind: Optional[str] = None      # objective/subjective
    evaluator: Optional[str] = None
    description: Optional[str] = None
    cause: Optional[str] = None            # 关联事件名（可空）
    # 0.7.0 P2：trigger 候选字段
    trigger_type: Optional[str] = None
    trigger_text: Optional[str] = None
    trigger_target_type: Optional[str] = None  # event/fact/belief/intent/impact


@dataclass
class WriteResult:
    accepted: int
    rejected: list
    ids: list
