"""AVIS database models derived from specs/database/POSTGRESQL_SPEC.md."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from avis.db.base import Base


class RefExchange(Base):
    __tablename__ = "ref_exchange"

    exchange_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    exchange_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    exchange_name: Mapped[str] = mapped_column(String(128), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instruments: Mapped[list[RefInstrument]] = relationship(back_populates="exchange")
    calendars: Mapped[list[RefCalendar]] = relationship(back_populates="exchange")

    __table_args__ = (
        Index("ix_ref_exchange_active", "is_active"),
    )


class RefCompany(Base):
    __tablename__ = "ref_company"

    company_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    legal_name: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    isin_primary: Mapped[str | None] = mapped_column(String(12), nullable=True)
    sector_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    industry_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    incorporation_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instruments: Mapped[list[RefInstrument]] = relationship(back_populates="company")
    news_entity_links: Mapped[list[NewsEntityLink]] = relationship(back_populates="company")

    __table_args__ = (
        Index("ix_ref_company_display_name", "display_name"),
        Index("ix_ref_company_sector_industry", "sector_code", "industry_code"),
    )


class RefInstrument(Base):
    __tablename__ = "ref_instrument"

    instrument_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("ref_company.company_id"), nullable=False)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("ref_exchange.exchange_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    company: Mapped[RefCompany] = relationship(back_populates="instruments")
    exchange: Mapped[RefExchange] = relationship(back_populates="instruments")
    symbol_aliases: Mapped[list[RefSymbolAlias]] = relationship(back_populates="instrument")

    market_ohlcv_1d_rows: Mapped[list[MarketOhlcv1D]] = relationship(back_populates="instrument")
    market_ohlcv_intraday_rows: Mapped[list[MarketOhlcvIntraday]] = relationship(back_populates="instrument")
    market_corporate_actions: Mapped[list[MarketCorporateAction]] = relationship(back_populates="instrument")
    market_orderbook_snapshots: Mapped[list[MarketOrderbookSnapshot]] = relationship(back_populates="instrument")

    fund_statement_facts: Mapped[list[FundStatementFact]] = relationship(back_populates="instrument")
    fund_metric_facts: Mapped[list[FundMetricFact]] = relationship(back_populates="instrument")
    fund_segment_facts: Mapped[list[FundSegmentFact]] = relationship(back_populates="instrument")

    val_runs: Mapped[list[ValRun]] = relationship(back_populates="instrument")

    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", "is_active", name="ux_ref_instrument_exchange_symbol_active"),
        Index("ix_ref_instrument_company", "company_id"),
        CheckConstraint(
            "instrument_type IN ('EQUITY','ETF','ADR','PREF')",
            name="instrument_type_allowed",
        ),
    )


class RefSymbolAlias(Base):
    __tablename__ = "ref_symbol_alias"

    alias_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    alias_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="symbol_aliases")

    __table_args__ = (
        Index("ix_ref_symbol_alias_inst", "instrument_id"),
        Index("ix_ref_symbol_alias_source_symbol", "source_system", "alias_symbol"),
        Index("ix_ref_symbol_alias_validity", "valid_from", "valid_to"),
    )


class RefCalendar(Base):
    __tablename__ = "ref_calendar"

    calendar_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("ref_exchange.exchange_id"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session_open_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_close_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    holiday_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    exchange: Mapped[RefExchange] = relationship(back_populates="calendars")

    __table_args__ = (
        UniqueConstraint("exchange_id", "trade_date", name="ux_ref_calendar_exchange_date"),
        Index("ix_ref_calendar_trading_day", "is_trading_day"),
    )


class MarketOhlcv1D(Base):
    __tablename__ = "market_ohlcv_1d"

    ohlcv_1d_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    adj_close_px: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 0), nullable=False)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="market_ohlcv_1d_rows")

    __table_args__ = (
        UniqueConstraint("instrument_id", "trade_date", name="ux_market_ohlcv_1d_inst_date"),
        Index("ix_market_ohlcv_1d_date", "trade_date"),
        CheckConstraint("high_px >= GREATEST(open_px, close_px)", name="market_ohlcv_1d_high_valid"),
        CheckConstraint("low_px <= LEAST(open_px, close_px)", name="market_ohlcv_1d_low_valid"),
    )


class MarketOhlcvIntraday(Base):
    __tablename__ = "market_ohlcv_intraday"

    ohlcv_intraday_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    bar_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_interval_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    open_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close_px: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 0), nullable=False)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="market_ohlcv_intraday_rows")

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "bar_start_ts",
            "bar_interval_sec",
            name="ux_market_intraday_inst_start_interval",
        ),
        Index("ix_market_intraday_time", "bar_start_ts"),
        CheckConstraint("bar_end_ts > bar_start_ts", name="market_intraday_valid_range"),
    )


class MarketCorporateAction(Base):
    __tablename__ = "market_corporate_action"

    corp_action_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    action_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    action_ratio_num: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    action_ratio_den: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="market_corporate_actions")

    __table_args__ = (
        Index("ix_market_corp_action_inst_exdate", "instrument_id", "ex_date"),
        Index("ix_market_corp_action_type", "action_type"),
        CheckConstraint(
            "action_type IN ('DIVIDEND','SPLIT','BONUS','RIGHTS','MERGER','DEMERGER','DELISTING')",
            name="market_corp_action_type_allowed",
        ),
    )


class MarketOrderbookSnapshot(Base):
    __tablename__ = "market_orderbook_snapshot"

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    depth_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    best_bid_px: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    best_bid_qty: Mapped[Decimal | None] = mapped_column(Numeric(24, 0), nullable=True)
    best_ask_px: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    best_ask_qty: Mapped[Decimal | None] = mapped_column(Numeric(24, 0), nullable=True)
    book_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="market_orderbook_snapshots")

    __table_args__ = (
        Index("ix_market_orderbook_inst_ts", "instrument_id", "snapshot_ts"),
        Index("gin_market_orderbook_payload", "book_payload", postgresql_using="gin"),
        CheckConstraint("depth_level >= 1", name="market_orderbook_depth_positive"),
    )


class FundStatementFact(Base):
    __tablename__ = "fund_statement_fact"

    statement_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(8), nullable=False)
    statement_type: Mapped[str] = mapped_column(String(8), nullable=False)
    line_item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    scale_code: Mapped[str] = mapped_column(String(16), nullable=False)
    as_reported_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="fund_statement_facts")
    restatements: Mapped[list[FundRestatementLog]] = relationship(back_populates="statement_fact")

    __table_args__ = (
        Index("ix_fund_statement_inst_period", "instrument_id", "fiscal_year", "fiscal_period"),
        Index("ix_fund_statement_line_item", "line_item_code"),
        Index("ix_fund_statement_effective_window", "effective_from_ts", "effective_to_ts"),
        CheckConstraint("statement_type IN ('PL','BS','CF')", name="fund_statement_type_allowed"),
        CheckConstraint(
            "fiscal_period IN ('Q1','Q2','Q3','Q4','FY','H1','H2','TTM')",
            name="fund_fiscal_period_allowed",
        ),
    )


class FundMetricFact(Base):
    __tablename__ = "fund_metric_fact"

    metric_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    metric_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    lookback_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="fund_metric_facts")

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "metric_code",
            "as_of_date",
            "calc_version",
            name="ux_fund_metric_inst_metric_asof_version",
        ),
        Index("ix_fund_metric_metric_code", "metric_code"),
    )


class FundRestatementLog(Base):
    __tablename__ = "fund_restatement_log"

    restatement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    statement_fact_id: Mapped[int] = mapped_column(
        ForeignKey("fund_statement_fact.statement_fact_id"), nullable=False
    )
    restatement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    new_value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)

    statement_fact: Mapped[FundStatementFact] = relationship(back_populates="restatements")

    __table_args__ = (
        Index("ix_fund_restatement_fact", "statement_fact_id"),
        Index("ix_fund_restatement_detected", "detected_at"),
        CheckConstraint("new_value <> old_value", name="fund_restatement_value_changed"),
    )


class FundSegmentFact(Base):
    __tablename__ = "fund_segment_fact"

    segment_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    segment_code: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(8), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    as_reported_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    instrument: Mapped[RefInstrument] = relationship(back_populates="fund_segment_facts")

    __table_args__ = (
        Index(
            "ix_fund_segment_inst_segment_period",
            "instrument_id",
            "segment_code",
            "fiscal_year",
            "fiscal_period",
        ),
        Index("ix_fund_segment_metric", "metric_code"),
    )


class NewsSourceProfile(Base):
    __tablename__ = "news_source_profile"

    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    license_class: Mapped[str] = mapped_column(String(32), nullable=False)
    base_reliability_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    terms_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    articles: Mapped[list[NewsArticle]] = relationship(back_populates="source")

    __table_args__ = (
        Index("ix_news_source_active", "is_active"),
        CheckConstraint(
            "base_reliability_score >= 0 AND base_reliability_score <= 100",
            name="news_source_base_reliability_range",
        ),
    )


class NewsArticle(Base):
    __tablename__ = "news_article"

    article_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("news_source_profile.source_id"), nullable=False)
    external_article_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    article_text_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    article_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_paywalled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source: Mapped[NewsSourceProfile] = relationship(back_populates="articles")
    entity_links: Mapped[list[NewsEntityLink]] = relationship(back_populates="article")
    events: Mapped[list[NewsEvent]] = relationship(back_populates="article")
    reliability_scores: Mapped[list[NewsReliabilityScore]] = relationship(back_populates="article")

    __table_args__ = (
        UniqueConstraint("source_id", "external_article_id", name="ux_news_article_source_external"),
        Index("ix_news_article_published_at", "published_at"),
        Index("ix_news_article_hash", "article_text_hash"),
        CheckConstraint("captured_at >= published_at", name="news_article_capture_after_publish"),
    )


class NewsEntityLink(Base):
    __tablename__ = "news_entity_link"

    entity_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_article.article_id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("ref_company.company_id"), nullable=False)
    mention_type: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    article: Mapped[NewsArticle] = relationship(back_populates="entity_links")
    company: Mapped[RefCompany] = relationship(back_populates="news_entity_links")

    __table_args__ = (
        UniqueConstraint("article_id", "company_id", "mention_type", name="ux_news_entity_unique"),
        Index("ix_news_entity_company", "company_id"),
        CheckConstraint("relevance_score >= 0 AND relevance_score <= 100", name="news_entity_relevance_range"),
    )


class NewsEvent(Base):
    __tablename__ = "news_event"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_article.article_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    event_severity: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    event_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    event_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    article: Mapped[NewsArticle] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_news_event_type", "event_type"),
        Index("ix_news_event_article", "article_id"),
        Index("gin_news_event_payload", "event_payload", postgresql_using="gin"),
        CheckConstraint("event_confidence >= 0 AND event_confidence <= 100", name="news_event_confidence_range"),
        CheckConstraint(
            "event_direction IS NULL OR event_direction IN ('POSITIVE','NEGATIVE','NEUTRAL')",
            name="news_event_direction_allowed",
        ),
    )


class NewsReliabilityScore(Base):
    __tablename__ = "news_reliability_score"

    reliability_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_article.article_id"), nullable=False)
    source_reliability: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    content_consistency: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    cross_source_confirmation: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    final_reliability_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    article: Mapped[NewsArticle] = relationship(back_populates="reliability_scores")

    __table_args__ = (
        UniqueConstraint("article_id", "scoring_version", name="ux_news_rel_article_version"),
        Index("ix_news_rel_final_score", "final_reliability_score"),
        CheckConstraint(
            "source_reliability >= 0 AND source_reliability <= 100",
            name="news_rel_source_reliability_range",
        ),
        CheckConstraint(
            "content_consistency >= 0 AND content_consistency <= 100",
            name="news_rel_content_consistency_range",
        ),
        CheckConstraint(
            "cross_source_confirmation >= 0 AND cross_source_confirmation <= 100",
            name="news_rel_cross_source_confirmation_range",
        ),
        CheckConstraint(
            "final_reliability_score >= 0 AND final_reliability_score <= 100",
            name="news_rel_final_score_range",
        ),
    )


class OpsPipelineRun(Base):
    __tablename__ = "ops_pipeline_run"

    pipeline_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    pipeline_name: Mapped[str] = mapped_column(String(128), nullable=False)
    run_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    job_events: Mapped[list[OpsJobEvent]] = relationship(back_populates="pipeline_run")
    lineage_rows: Mapped[list[OpsDataLineage]] = relationship(back_populates="pipeline_run")
    sla_breaches: Mapped[list[OpsSlaBreach]] = relationship(back_populates="pipeline_run")
    dq_results: Mapped[list[DqResult]] = relationship(back_populates="pipeline_run")
    val_runs: Mapped[list[ValRun]] = relationship(back_populates="pipeline_run")

    __table_args__ = (
        Index("ix_ops_pipeline_name_started", "pipeline_name", "started_at"),
        Index("ix_ops_pipeline_status", "status"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCESS','FAILED','CANCELLED')",
            name="ops_pipeline_status_allowed",
        ),
    )


class ValRun(Base):
    __tablename__ = "val_run"

    val_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("ref_instrument.instrument_id"), nullable=False)
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("ops_pipeline_run.pipeline_run_id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instrument: Mapped[RefInstrument] = relationship(back_populates="val_runs")
    pipeline_run: Mapped[OpsPipelineRun | None] = relationship(back_populates="val_runs")
    assumptions: Mapped[list[ValAssumptionSet]] = relationship(back_populates="val_run")
    model_outputs: Mapped[list[ValModelOutput]] = relationship(back_populates="val_run")
    attributions: Mapped[list[ValAttribution]] = relationship(back_populates="val_run")
    confidence_snapshots: Mapped[list[ValConfidenceSnapshot]] = relationship(back_populates="val_run")

    __table_args__ = (
        Index("ix_val_run_inst_asof", "instrument_id", "as_of_ts"),
        Index("ix_val_run_status", "status"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCESS','FAILED','OVERRIDDEN')",
            name="val_run_status_allowed",
        ),
    )


class ValAssumptionSet(Base):
    __tablename__ = "val_assumption_set"

    assumption_set_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    val_run_id: Mapped[int] = mapped_column(ForeignKey("val_run.val_run_id"), nullable=False)
    assumption_key: Mapped[str] = mapped_column(String(128), nullable=False)
    assumption_value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    assumption_value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    assumption_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    val_run: Mapped[ValRun] = relationship(back_populates="assumptions")

    __table_args__ = (
        UniqueConstraint("val_run_id", "assumption_key", name="ux_val_assumption_unique"),
        Index("ix_val_assumption_source_type", "source_type"),
        CheckConstraint(
            "assumption_value_numeric IS NOT NULL OR assumption_value_text IS NOT NULL",
            name="val_assumption_one_value_present",
        ),
    )


class ValModelOutput(Base):
    __tablename__ = "val_model_output"

    model_output_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    val_run_id: Mapped[int] = mapped_column(ForeignKey("val_run.val_run_id"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    equity_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    enterprise_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    val_run: Mapped[ValRun] = relationship(back_populates="model_outputs")
    sensitivities: Mapped[list[ValSensitivity]] = relationship(back_populates="model_output")

    __table_args__ = (
        UniqueConstraint("val_run_id", "model_name", "model_version", name="ux_val_model_run_name_version"),
        Index("ix_val_model_name", "model_name"),
        CheckConstraint(
            "model_name IN ('DCF','RELATIVE','SOTP','BLENDED','SCENARIO_WEIGHTED')",
            name="val_model_name_allowed",
        ),
    )


class ValSensitivity(Base):
    __tablename__ = "val_sensitivity"

    sensitivity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_output_id: Mapped[int] = mapped_column(ForeignKey("val_model_output.model_output_id"), nullable=False)
    assumption_key: Mapped[str] = mapped_column(String(128), nullable=False)
    shock_label: Mapped[str] = mapped_column(String(64), nullable=False)
    shock_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    result_target_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    model_output: Mapped[ValModelOutput] = relationship(back_populates="sensitivities")

    __table_args__ = (
        Index("ix_val_sens_model_output", "model_output_id"),
        Index("ix_val_sens_assumption_key", "assumption_key"),
        UniqueConstraint(
            "model_output_id",
            "assumption_key",
            "shock_label",
            name="ux_val_sens_model_assumption_shock",
        ),
    )


class ValAttribution(Base):
    __tablename__ = "val_attribution"

    attribution_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    val_run_id: Mapped[int] = mapped_column(ForeignKey("val_run.val_run_id"), nullable=False)
    driver_type: Mapped[str] = mapped_column(String(64), nullable=False)
    driver_key: Mapped[str] = mapped_column(String(128), nullable=False)
    impact_value_abs: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    impact_value_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    val_run: Mapped[ValRun] = relationship(back_populates="attributions")

    __table_args__ = (
        Index("ix_val_attr_run", "val_run_id"),
        Index("ix_val_attr_driver", "driver_type", "driver_key"),
        CheckConstraint("direction IN ('UP','DOWN','NEUTRAL')", name="val_attr_direction_allowed"),
    )


class ValConfidenceSnapshot(Base):
    __tablename__ = "val_confidence_snapshot"

    confidence_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    val_run_id: Mapped[int] = mapped_column(ForeignKey("val_run.val_run_id"), nullable=False)
    overall_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    data_quality_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    management_credibility_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    industry_stability_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    forecast_accuracy_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    news_reliability_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    model_agreement_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    scenario_dispersion_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    val_run: Mapped[ValRun] = relationship(back_populates="confidence_snapshots")

    __table_args__ = (
        UniqueConstraint("val_run_id", "scoring_version", name="ux_val_conf_run_version"),
        Index("ix_val_conf_overall", "overall_confidence"),
        CheckConstraint("overall_confidence >= 0 AND overall_confidence <= 100", name="val_conf_overall_range"),
        CheckConstraint("data_quality_score >= 0 AND data_quality_score <= 100", name="val_conf_data_quality_range"),
        CheckConstraint(
            "management_credibility_score >= 0 AND management_credibility_score <= 100",
            name="val_conf_management_credibility_range",
        ),
        CheckConstraint(
            "industry_stability_score >= 0 AND industry_stability_score <= 100",
            name="val_conf_industry_stability_range",
        ),
        CheckConstraint(
            "forecast_accuracy_score >= 0 AND forecast_accuracy_score <= 100",
            name="val_conf_forecast_accuracy_range",
        ),
        CheckConstraint(
            "news_reliability_score >= 0 AND news_reliability_score <= 100",
            name="val_conf_news_reliability_range",
        ),
        CheckConstraint(
            "model_agreement_score >= 0 AND model_agreement_score <= 100",
            name="val_conf_model_agreement_range",
        ),
        CheckConstraint(
            "scenario_dispersion_score >= 0 AND scenario_dispersion_score <= 100",
            name="val_conf_scenario_dispersion_range",
        ),
    )


class DqRule(Base):
    __tablename__ = "dq_rule"

    dq_rule_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    rule_name: Mapped[str] = mapped_column(String(256), nullable=False)
    domain_name: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_expression: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner_team: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    results: Mapped[list[DqResult]] = relationship(back_populates="rule")

    __table_args__ = (
        Index("ix_dq_rule_domain_severity", "domain_name", "severity"),
        CheckConstraint(
            "severity IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')",
            name="dq_rule_severity_allowed",
        ),
    )


class DqResult(Base):
    __tablename__ = "dq_result"

    dq_result_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dq_rule_id: Mapped[int] = mapped_column(ForeignKey("dq_rule.dq_rule_id"), nullable=False)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("ops_pipeline_run.pipeline_run_id"), nullable=True)
    target_table: Mapped[str] = mapped_column(String(128), nullable=False)
    target_record_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    measured_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    threshold_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    rule: Mapped[DqRule] = relationship(back_populates="results")
    pipeline_run: Mapped[OpsPipelineRun | None] = relationship(back_populates="dq_results")
    incidents: Mapped[list[DqIncident]] = relationship(back_populates="dq_result")

    __table_args__ = (
        Index("ix_dq_result_rule_time", "dq_rule_id", "evaluated_at"),
        Index("ix_dq_result_status", "status"),
        CheckConstraint("status IN ('PASS','FAIL','WARN','SKIPPED')", name="dq_result_status_allowed"),
    )


class DqIncident(Base):
    __tablename__ = "dq_incident"

    dq_incident_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dq_result_id: Mapped[int] = mapped_column(ForeignKey("dq_result.dq_result_id"), nullable=False)
    incident_status: Mapped[str] = mapped_column(String(16), nullable=False)
    impact_level: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    dq_result: Mapped[DqResult] = relationship(back_populates="incidents")
    overrides: Mapped[list[DqOverride]] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_dq_incident_status", "incident_status"),
        Index("ix_dq_incident_opened", "opened_at"),
        CheckConstraint(
            "incident_status IN ('OPEN','IN_PROGRESS','RESOLVED','CLOSED')",
            name="dq_incident_status_allowed",
        ),
    )


class AuthUser(Base):
    __tablename__ = "auth_user"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    is_mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user_roles: Mapped[list[AuthUserRole]] = relationship(
        back_populates="user",
        foreign_keys="AuthUserRole.user_id",
    )
    granted_roles: Mapped[list[AuthUserRole]] = relationship(
        back_populates="granted_by_user",
        foreign_keys="AuthUserRole.granted_by_user_id",
    )
    dq_overrides: Mapped[list[DqOverride]] = relationship(back_populates="approved_by_user")
    audit_events: Mapped[list[AuthAuditEvent]] = relationship(back_populates="user")

    __table_args__ = (
        Index("ix_auth_user_status", "status"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','LOCKED')", name="auth_user_status_allowed"),
    )


class DqOverride(Base):
    __tablename__ = "dq_override"

    dq_override_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dq_incident_id: Mapped[int] = mapped_column(ForeignKey("dq_incident.dq_incident_id"), nullable=False)
    approved_by_user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.user_id"), nullable=False)
    override_reason: Mapped[str] = mapped_column(Text, nullable=False)
    override_expiry_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    incident: Mapped[DqIncident] = relationship(back_populates="overrides")
    approved_by_user: Mapped[AuthUser] = relationship(back_populates="dq_overrides")

    __table_args__ = (
        Index("ix_dq_override_incident", "dq_incident_id"),
        Index("ix_dq_override_expiry", "override_expiry_ts"),
    )


class OpsJobEvent(Base):
    __tablename__ = "ops_job_event"

    job_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pipeline_run_id: Mapped[int] = mapped_column(ForeignKey("ops_pipeline_run.pipeline_run_id"), nullable=False)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    pipeline_run: Mapped[OpsPipelineRun] = relationship(back_populates="job_events")

    __table_args__ = (
        Index("ix_ops_job_event_run", "pipeline_run_id"),
        Index("ix_ops_job_event_type_ts", "event_type", "event_ts"),
        CheckConstraint("event_type IN ('START','END','WARN','ERROR','RETRY')", name="ops_job_event_type_allowed"),
    )


class OpsDataLineage(Base):
    __tablename__ = "ops_data_lineage"

    lineage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pipeline_run_id: Mapped[int] = mapped_column(ForeignKey("ops_pipeline_run.pipeline_run_id"), nullable=False)
    source_asset: Mapped[str] = mapped_column(String(256), nullable=False)
    target_asset: Mapped[str] = mapped_column(String(256), nullable=False)
    transform_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record_count_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    record_count_out: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lineage_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pipeline_run: Mapped[OpsPipelineRun] = relationship(back_populates="lineage_rows")

    __table_args__ = (
        Index("ix_ops_lineage_run", "pipeline_run_id"),
        Index("ix_ops_lineage_assets", "source_asset", "target_asset"),
        CheckConstraint(
            "record_count_in IS NULL OR record_count_in >= 0",
            name="ops_lineage_record_count_in_non_negative",
        ),
        CheckConstraint(
            "record_count_out IS NULL OR record_count_out >= 0",
            name="ops_lineage_record_count_out_non_negative",
        ),
    )


class OpsSlaBreach(Base):
    __tablename__ = "ops_sla_breach"

    sla_breach_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("ops_pipeline_run.pipeline_run_id"), nullable=True)
    sla_type: Mapped[str] = mapped_column(String(32), nullable=False)
    service_name: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    breach_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    pipeline_run: Mapped[OpsPipelineRun | None] = relationship(back_populates="sla_breaches")

    __table_args__ = (
        Index("ix_ops_sla_service_type", "service_name", "sla_type"),
        Index("ix_ops_sla_status", "status"),
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ops_sla_status_allowed"),
    )


class AuthRole(Base):
    __tablename__ = "auth_role"

    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user_roles: Mapped[list[AuthUserRole]] = relationship(back_populates="role")
    permissions: Mapped[list[AuthPermission]] = relationship(back_populates="role")


class AuthPermission(Base):
    __tablename__ = "auth_permission"

    permission_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("auth_role.role_id"), nullable=False)
    resource_code: Mapped[str] = mapped_column(String(128), nullable=False)
    action_code: Mapped[str] = mapped_column(String(64), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    role: Mapped[AuthRole] = relationship(back_populates="permissions")

    __table_args__ = (
        UniqueConstraint(
            "role_id",
            "resource_code",
            "action_code",
            name="ux_auth_permission_role_resource_action",
        ),
        Index("ix_auth_permission_resource", "resource_code"),
        CheckConstraint("effect IN ('ALLOW','DENY')", name="auth_permission_effect_allowed"),
    )


class AuthUserRole(Base):
    __tablename__ = "auth_user_role"

    user_role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.user_id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("auth_role.role_id"), nullable=False)
    granted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("auth_user.user_id"), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[AuthUser] = relationship(back_populates="user_roles", foreign_keys=[user_id])
    granted_by_user: Mapped[AuthUser | None] = relationship(
        back_populates="granted_roles",
        foreign_keys=[granted_by_user_id],
    )
    role: Mapped[AuthRole] = relationship(back_populates="user_roles")

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "valid_from", name="ux_auth_user_role_unique_window"),
        Index("ix_auth_user_role_user", "user_id"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="auth_user_role_valid_window"),
    )


class AuthAuditEvent(Base):
    __tablename__ = "auth_audit_event"

    audit_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("auth_user.user_id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    user: Mapped[AuthUser | None] = relationship(back_populates="audit_events")

    __table_args__ = (
        Index("ix_auth_audit_user_ts", "user_id", "event_ts"),
        Index("ix_auth_audit_event_type", "event_type"),
        Index("gin_auth_audit_payload", "event_payload", postgresql_using="gin"),
    )
