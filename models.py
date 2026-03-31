from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from database import db

class Province(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    districts = db.relationship('District', backref='province', lazy=True)

class District(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    province_id = db.Column(db.Integer, db.ForeignKey('province.id'), nullable=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    role = db.Column(db.String(20), default='staff')
    district_id = db.Column(db.Integer, db.ForeignKey('district.id'), nullable=True)
    province_id = db.Column(db.Integer, db.ForeignKey('province.id'), nullable=True)

    district = db.relationship('District', foreign_keys=[district_id], backref='users')
    province = db.relationship('Province', foreign_keys=[province_id], backref='users')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class ServicePoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    order = db.Column(db.Integer, nullable=False)
    default_service = db.Column(db.String(80), nullable=False)
    district_id = db.Column(db.Integer, db.ForeignKey('district.id'), nullable=False)

    district = db.relationship('District', backref='service_points')

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    arrival_time = db.Column(db.DateTime, default=datetime.now, nullable=False)
    exit_time = db.Column(db.DateTime, nullable=True)
    district_id = db.Column(db.Integer, db.ForeignKey('district.id'), nullable=False)

    services = db.relationship('Service', backref='patient', lazy=True,
                               cascade="all, delete-orphan", order_by="Service.start_time")
    surveys = db.relationship('Survey', backref='patient', lazy=True,
                              cascade="all, delete-orphan")
    exits = db.relationship('ExitLog', backref='patient', lazy=True,
                            cascade="all, delete-orphan")
    district = db.relationship('District', backref='patients')

class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    service_point_id = db.Column(db.Integer, db.ForeignKey('service_point.id'), nullable=False)
    service_point = db.relationship('ServicePoint')
    service_name = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    waiting_time = db.Column(db.Float, default=0)
    needs_next_service = db.Column(db.Boolean, default=False)

class Survey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=True)
    district_id = db.Column(db.Integer, db.ForeignKey('district.id'), nullable=True)  # NEW
    satisfaction = db.Column(db.Boolean, nullable=False)
    received_services = db.Column(db.Boolean, nullable=False)
    courteous = db.Column(db.Boolean, nullable=False)
    speed = db.Column(db.Boolean, nullable=False)
    comments = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.now)

class ExitLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)