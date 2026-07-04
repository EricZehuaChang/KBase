from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON: 分块/增强配置


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    # pending -> parsing -> ready | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # MonkeyOCR 无置信度信号，固定 1.0=未知；后续做质量门控时勿当作高置信
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prev_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    heading_path: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=True)
    enrich_context: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conv_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))            # user | assistant
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProviderRow(Base):
    __tablename__ = "providers"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_env: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    max_concurrency: Mapped[int] = mapped_column(default=4)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
