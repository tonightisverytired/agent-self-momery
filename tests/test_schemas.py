# -*- coding: utf-8 -*-
"""0.8.0 B-01-T 请求 Schema 全集校验。"""
import pytest
from pydantic import ValidationError as PydanticValidationError

from server.api.schemas import (BeliefCreate, EdgeCreate, EntityCreate,
                                EventCreate, EvidenceCreate, FactCreate,
                                ImpactCreate, IntentCreate, ReflectRequest)


def test_belief_bad_polarity():
    with pytest.raises(PydanticValidationError):
        BeliefCreate(subject="用户", proposition="x", polarity="angry")


def test_intent_bad_status():
    with pytest.raises(PydanticValidationError):
        IntentCreate(subject="用户", proposition="x", status="doing")


def test_impact_bad_direction_valence():
    with pytest.raises(PydanticValidationError):
        ImpactCreate(subject="用户", dimension="income", direction="up",
                     valence="positive")
    with pytest.raises(PydanticValidationError):
        ImpactCreate(subject="用户", dimension="income", direction="increase",
                     valence="good")
    # 自定义维度不拒（文档 §7.1）
    ImpactCreate(subject="用户", dimension="commute", direction="decrease",
                 valence="negative")


def test_magnitude_confidence_range():
    with pytest.raises(PydanticValidationError):
        ImpactCreate(subject="用户", dimension="income", direction="increase",
                     valence="positive", magnitude=1.5)
    with pytest.raises(PydanticValidationError):
        FactCreate(entity="用户", key="x", value="y", confidence=2.0)
    with pytest.raises(PydanticValidationError):
        BeliefCreate(subject="用户", proposition="x", confidence=-0.1)


def test_evidence_bad_source_type():
    with pytest.raises(PydanticValidationError):
        EvidenceCreate(source_type="gossip")


def test_edge_bad_rel():
    with pytest.raises(PydanticValidationError):
        EdgeCreate(a="用户", b="咖啡", rel="not_a_rel")


def test_extra_field_forbidden():
    with pytest.raises(PydanticValidationError):
        EntityCreate(name="用户", kind="person", hack="x")
    with pytest.raises(PydanticValidationError):
        EventCreate(name="事件", extra_field=1)


def test_govern_schemas():
    with pytest.raises(PydanticValidationError):
        ReflectRequest(year=2026, month=13)
    ReflectRequest(year=2026, month=9)
