from sqlalchemy import Column, Integer, String, Date, Time, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    full_name = Column(String(150), nullable=True)


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    specialty = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    active = Column(Boolean, default=True)

    schedules = relationship("DoctorSchedule", back_populates="doctor", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="doctor")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    category = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    price = Column(String(50), nullable=True)
    active = Column(Boolean, default=True)

    bookings = relationship("Booking", back_populates="service")


class Promotion(Base):
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    detail = Column(Text, nullable=True)
    price = Column(String(50), nullable=True)
    badge = Column(String(100), nullable=True)
    active = Column(Boolean, default=True)


class DoctorSchedule(Base):
    __tablename__ = "doctor_schedules"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    work_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    available = Column(Boolean, default=True)

    doctor = relationship("Doctor", back_populates="schedules")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    customer_name = Column(String(150), nullable=False)
    phone = Column(String(50), nullable=False)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    note = Column(Text, nullable=True)
    is_consultation = Column(Boolean, default=False)
    booking_date = Column(Date, nullable=False)
    booking_time = Column(Time, nullable=False)
    status = Column(String(50), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="bookings")
    service = relationship("Service", back_populates="bookings")