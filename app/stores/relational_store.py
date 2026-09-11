"""
Relational store: source registry + fact audit trail + gap log + reports.

This is the GOVERNANCE layer. It answers "was this sourced legitimately,
when, by what decision, and why" — questions with exact answers, asked
about individual rows, which is precisely what a relational store is good
at and what the other two are not. The vector store can tell you what a
city's documents talk about; the graph can tell you how entities relate;
only this one can tell you that a specific URL was refused on 11 Sep at
16:04 because its robots.txt disallowed the path.

It is also the only store that keeps DENIED sources. Recording what we
deliberately did not read is part of the evidence trail: a reviewer can
see the gate ran and what it cost us, rather than having to trust that it
did.

SQLite by default (zero setup); point DATABASE_URL at Postgres for
production without changing a line of calling code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings
from app.stores.lazy import LazyStore
from app.util import domain_of

Base = declarative_base()


class SourceRecord(Base):
    __tablename__ = "sources"
    url = Column(String, primary_key=True)
    domain = Column(String, index=True)
    city = Column(String, index=True)
    dimension = Column(String)
    title = Column(Text)
    crawl_verdict = Column(String)        # ALLOWED / DENIED
    crawl_reason = Column(Text)
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class FactAuditRecord(Base):
    __tablename__ = "fact_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    claim_id = Column(String, index=True)
    city = Column(String, index=True)
    dimension = Column(String, index=True)
    text = Column(Text)
    tier = Column(String)                 # VERIFIED / SINGLE_SOURCE / CONFLICTING / UNSUPPORTED
    reasoning = Column(Text)
    national_vs_city_flag = Column(Boolean, default=False)
    source_url = Column(Text)             # the passage the claim came from
    corroborating_urls = Column(Text)     # newline-joined; independent support only
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_by_human = Column(Boolean, default=False)  # hook for a future review queue


class GapRecord(Base):
    __tablename__ = "gaps"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String, index=True)
    dimension = Column(String)
    description = Column(Text)
    reason = Column(Text)
    logged_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ReportRecord(Base):
    """The generated brief, keyed by city so the most recent run is always
    retrievable. This is what makes the report downloadable after the fact
    and what /ask cites alongside raw passages — without it, a report exists
    only in the HTTP response that produced it."""
    __tablename__ = "reports"
    city = Column(String, primary_key=True)
    dimensions = Column(Text)
    run_mode = Column(String)
    markdown = Column(Text)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class RelationalStore:
    def __init__(self):
        self.engine = create_engine(settings.DATABASE_URL, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def record_sources(self, city: str, crawl_results, candidates_by_url: dict):
        with self.Session() as session:
            for result in crawl_results:
                candidate = candidates_by_url.get(result.url)
                session.merge(
                    SourceRecord(
                        url=result.url,
                        domain=domain_of(result.url),
                        city=city,
                        dimension=candidate.dimension if candidate else "",
                        title=candidate.title if candidate else "",
                        crawl_verdict=result.verdict.value,
                        crawl_reason=result.reason,
                    )
                )
            session.commit()

    def record_facts(self, city: str, fact_checked_claims):
        with self.Session() as session:
            for fc in fact_checked_claims:
                session.add(
                    FactAuditRecord(
                        claim_id=fc.claim_id,
                        city=city,
                        dimension=fc.dimension,
                        text=fc.text,
                        tier=fc.tier.value,
                        reasoning=fc.reasoning,
                        national_vs_city_flag=fc.national_vs_city_flag,
                        source_url=fc.source_url,
                        corroborating_urls="\n".join(fc.corroborating_urls),
                    )
                )
            session.commit()

    def record_gaps(self, city: str, gaps):
        with self.Session() as session:
            for gap in gaps:
                session.add(
                    GapRecord(
                        city=city,
                        dimension=gap.dimension,
                        description=gap.description,
                        reason=gap.reason,
                    )
                )
            session.commit()

    def save_report(self, city: str, dimensions: list[str], markdown: str):
        with self.Session() as session:
            session.merge(
                ReportRecord(
                    city=city,
                    dimensions=",".join(dimensions),
                    run_mode=settings.RUN_MODE,
                    markdown=markdown,
                    generated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    # ------------------------------------------------------------------
    # Reads — these back the evidence-tracing endpoints
    # ------------------------------------------------------------------
    def get_report(self, city: str) -> dict | None:
        with self.Session() as session:
            record = session.get(ReportRecord, city)
            if not record:
                return None
            return {
                "city": record.city,
                "dimensions": record.dimensions.split(",") if record.dimensions else [],
                "run_mode": record.run_mode,
                "markdown": record.markdown,
                "generated_at": record.generated_at.isoformat() if record.generated_at else None,
            }

    def get_sources(self, city: str) -> list[dict]:
        """Every URL considered for this city and the gate's verdict on it —
        allowed and denied alike."""
        with self.Session() as session:
            records = (
                session.query(SourceRecord)
                .filter(SourceRecord.city == city)
                .order_by(SourceRecord.crawl_verdict.asc(), SourceRecord.domain.asc())
                .all()
            )
            return [
                {
                    "url": r.url,
                    "domain": r.domain,
                    "title": r.title,
                    "dimension": r.dimension,
                    "verdict": r.crawl_verdict,
                    "reason": r.crawl_reason,
                    "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
                }
                for r in records
            ]

    def get_facts(self, city: str) -> list[dict]:
        with self.Session() as session:
            records = (
                session.query(FactAuditRecord)
                .filter(FactAuditRecord.city == city)
                .order_by(FactAuditRecord.dimension.asc())
                .all()
            )
            return [
                {
                    "claim_id": r.claim_id,
                    "dimension": r.dimension,
                    "text": r.text,
                    "tier": r.tier,
                    "reasoning": r.reasoning,
                    "national_vs_city_flag": bool(r.national_vs_city_flag),
                    "source_url": r.source_url,
                    "corroborating_urls": r.corroborating_urls.split("\n") if r.corroborating_urls else [],
                    "checked_at": r.checked_at.isoformat() if r.checked_at else None,
                    "reviewed_by_human": bool(r.reviewed_by_human),
                }
                for r in records
            ]

    def get_gaps(self, city: str) -> list[dict]:
        with self.Session() as session:
            records = session.query(GapRecord).filter(GapRecord.city == city).all()
            return [
                {
                    "dimension": r.dimension,
                    "description": r.description,
                    "reason": r.reason,
                    "logged_at": r.logged_at.isoformat() if r.logged_at else None,
                }
                for r in records
            ]

    def list_cities(self) -> list[dict]:
        """Powers the 'already researched' list — the reusable intelligence
        asset the brief asks for is only reusable if you can see what is in it."""
        with self.Session() as session:
            records = session.query(ReportRecord).order_by(ReportRecord.generated_at.desc()).all()
            return [
                {
                    "city": r.city,
                    "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                }
                for r in records
            ]


relational_store = LazyStore(RelationalStore)
