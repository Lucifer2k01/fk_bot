"""SQLAlchemy database models."""
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, 
    DateTime, ForeignKey, Text, BigInteger, JSON
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from bot.config import settings

Base = declarative_base()
engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    accounts = relationship("FlipkartAccount", back_populates="user")
    products = relationship("Product", back_populates="user")
    cards = relationship("CreditCard", back_populates="user")
    orders = relationship("Order", back_populates="user")

class FlipkartAccount(Base):
    __tablename__ = "flipkart_accounts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_name = Column(String(100), default="Primary")
    cookies_encrypted = Column(Text, nullable=False)
    dc_preference = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    last_used = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="accounts")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("flipkart_accounts.id"))
    product_url = Column(Text, nullable=False)
    listing_id = Column(String(100))
    product_title = Column(String(500))
    current_price = Column(Float)
    target_price = Column(Float, default=0)
    quantity = Column(Integer, default=1)
    status = Column(String(50), default="tracking")
    payment_mode = Column(String(50), default="cod")
    bank_code = Column(String(50))
    card_id = Column(Integer, ForeignKey("credit_cards.id"))
    use_gst = Column(Boolean, default=True)
    use_supercoins = Column(Boolean, default=False)
    conditional_buy = Column(Boolean, default=False)
    check_interval = Column(Integer, default=5)
    next_check_at = Column(DateTime, default=datetime.utcnow)
    error_count = Column(Integer, default=0)
    last_error = Column(Text)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="products")
    orders = relationship("Order", back_populates="product")
    price_history = relationship(
        "PriceHistory",
        back_populates="product",
        cascade="all, delete-orphan"
    )

class CreditCard(Base):
    __tablename__ = "credit_cards"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    card_name = Column(String(100))
    number_encrypted = Column(Text, nullable=False)
    expiry_encrypted = Column(Text, nullable=False)
    cvv_encrypted = Column(Text, nullable=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="cards")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))
    flipkart_order_id = Column(String(100))
    status = Column(String(50), default="pending")
    total_amount = Column(Float)
    payment_mode = Column(String(50))
    bank_code = Column(String(50))
    response_data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="orders")
    product = relationship("Product", back_populates="orders")

class PriceHistory(Base):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float)
    in_stock = Column(Boolean, default=True)
    raw_data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="price_history")

def init_db():
    Base.metadata.create_all(bind=engine)