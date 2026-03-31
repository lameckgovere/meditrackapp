from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, flash, session
)
from flask_cors import CORS
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from io import BytesIO
from statistics import mean, median, mode
from sqlalchemy import func, extract
from sqlalchemy.orm import joinedload
import qrcode, base64
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import shutil
import os
import logging
from logging.handlers import RotatingFileHandler
from sqlalchemy import or_

from database import db
from models import Patient, Service, Survey, ExitLog, User, ServicePoint, District, Province

app = Flask(__name__)

# Environment-based configuration
if 'DATABASE_URL' in os.environ:
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///patients.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# Logging
handler = RotatingFileHandler('meditrack.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

db.init_app(app)
CORS(app)

# Template filters
@app.template_filter('format_min_sec')
def format_min_sec_filter(sec):
    if sec is None:
        return "0m 00s"
    try:
        m, s = divmod(int(sec), 60)
        return f"{m}m {s:02d}s"
    except (TypeError, ValueError):
        return "0m 00s"

@app.template_filter('format_time')
def format_time_filter(dt, format='%Y-%m-%d %H:%M'):
    if not dt:
        return ""
    return dt.strftime(format)

# Utilities
def get_current_time():
    return datetime.now()

def get_user_districts(user):
    """Return list of district IDs the user is allowed to see."""
    if user.role in ['admin', 'national']:
        return [d.id for d in District.query.all()]
    elif user.role == 'provincial':
        return [d.id for d in District.query.filter_by(province_id=user.province_id).all()]
    elif user.role == 'district_admin':
        return [user.district_id] if user.district_id else []
    else:
        return [user.district_id] if user.district_id else []

def generate_qr_code(data):
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#4361ee", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        img_str = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        app.logger.error(f"QR Generation Error: {e}")
        return ""

def get_service_time(service):
    if service.end_time and service.start_time:
        return (service.end_time - service.start_time).total_seconds()
    return 0

def calculate_patient_wait(patient, now):
    if patient.services:
        current_service = next((s for s in patient.services if s.start_time and not s.end_time), None)
        if current_service:
            return (now - current_service.start_time).total_seconds()
        else:
            completed = [s for s in patient.services if s.end_time]
            if completed:
                last_completed = max(completed, key=lambda s: s.end_time)
                return (now - last_completed.end_time).total_seconds()
    return (now - patient.arrival_time).total_seconds()

# Authentication
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        user = User.query.filter_by(username=u).first()
        if user and user.check_password(p):
            session['user_id'] = user.id
            session['role'] = user.role
            if user.district:
                session['district_id'] = user.district.id
                session['district_name'] = user.district.name
            if user.province:
                session['province_id'] = user.province.id
                session['province_name'] = user.province.name
            flash('Login successful!', 'success')
            if user.role == 'provincial':
                return redirect(url_for('provincial_dashboard'))
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'role' not in session or session['role'] != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def provincial_redirect(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'role' in session and session['role'] == 'provincial':
            return redirect(url_for('provincial_dashboard'))
        return f(*args, **kwargs)
    return decorated

# Main routes
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role == 'provincial':
        return redirect(url_for('provincial_dashboard'))
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@provincial_redirect
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))

    allowed_districts = get_user_districts(user)

    now = get_current_time()
    active_patients = db.session.query(Patient).options(
        joinedload(Patient.services).joinedload(Service.service_point)
    ).filter(
        Patient.exit_time.is_(None),
        Patient.district_id.in_(allowed_districts)
    ).all()

    for patient in active_patients:
        patient.current_wait = calculate_patient_wait(patient, now)

    today = now.date()
    start = datetime(today.year, today.month, today.day)
    end = start + timedelta(days=1)

    todays_patients = db.session.query(Patient).filter(
        Patient.arrival_time >= start,
        Patient.arrival_time < end,
        Patient.district_id.in_(allowed_districts)
    ).count()

    todays_surveys = db.session.query(Survey).filter(
        Survey.timestamp >= start,
        Survey.patient.has(Patient.district_id.in_(allowed_districts))
    ).count()

    sp_query = ServicePoint.query.filter(ServicePoint.district_id.in_(allowed_districts)).all()
    sp_ids = [sp.id for sp in sp_query]

    sp_status = db.session.query(
        Service.service_point_id, func.count(Service.id)
    ).filter(
        Service.end_time.is_(None),
        Service.service_point_id.in_(sp_ids)
    ).group_by(Service.service_point_id).all()

    sp_map = {sp.id: sp for sp in sp_query}
    service_points = []
    for id, cnt in sp_status:
        sp = sp_map.get(id)
        if sp:
            service_points.append({
                'id': sp.id,
                'name': sp.name,
                'queue_count': cnt,
                'active_count': Service.query.filter(
                    Service.service_point_id == sp.id,
                    Service.start_time.isnot(None),
                    Service.end_time.is_(None),
                    Service.needs_next_service == False
                ).count()
            })

    avg_wait_times = {}
    for sp in sp_query:
        services = Service.query.filter_by(service_point_id=sp.id).all()
        if services:
            wait_times = [s.waiting_time for s in services if s.waiting_time]
            avg_wait_times[sp.id] = mean(wait_times) if wait_times else 0

    return render_template(
        'dashboard.html',
        active_patients=active_patients,
        todays_patients=todays_patients,
        todays_surveys=todays_surveys,
        service_points=service_points,
        avg_wait_times=avg_wait_times,
        now=now
    )

@app.route('/patients')
@provincial_redirect
def patient_list():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    allowed_districts = get_user_districts(user)

    today = get_current_time().date()
    start = datetime(today.year, today.month, today.day)
    end = start + timedelta(days=1)

    patients = Patient.query.filter(
        Patient.arrival_time >= start,
        Patient.arrival_time < end,
        Patient.district_id.in_(allowed_districts)
    ).all()

    now = get_current_time()
    for patient in patients:
        patient.current_wait = calculate_patient_wait(patient, now)

    return render_template('patient_list.html', patients=patients)

@app.route('/patient/<int:id>')
@provincial_redirect
def patient_detail(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    patient = Patient.query.get_or_404(id)
    user = db.session.get(User, session['user_id'])
    allowed_districts = get_user_districts(user)
    if patient.district_id not in allowed_districts:
        flash('You do not have access to this patient', 'danger')
        return redirect(url_for('dashboard'))

    for service in patient.services:
        service.service_time = get_service_time(service)

    # Generate absolute URL for QR code to show patient details page
    patient_url = url_for('patient_detail', id=patient.id, _external=True)

    return render_template(
        'patient_detail.html',
        patient=patient,
        patient_url=patient_url,
        generate_qr_code=generate_qr_code
    )

# API
@app.route('/register', methods=['POST'])
def register_patient_api():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    user = db.session.get(User, session['user_id'])
    data = request.get_json()
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Missing name'}), 400

    try:
        if user.district_id is None:
            return jsonify({'error': 'User not assigned to any district'}), 400
        patient = Patient(name=name, arrival_time=get_current_time(), district_id=user.district_id)
        db.session.add(patient)
        db.session.commit()

        qr_code = generate_qr_code(f"patient:{patient.id}")
        first_sp = ServicePoint.query.filter_by(order=1, district_id=user.district_id).first()

        if first_sp:
            svc = Service(
                patient_id=patient.id,
                service_point_id=first_sp.id,
                service_name=first_sp.default_service
            )
            db.session.add(svc)
            db.session.commit()

        return jsonify({
            'id': patient.id,
            'name': patient.name,
            'arrival_time': patient.arrival_time.isoformat(),
            'qr_code': qr_code,
            'message': f"Patient {name} registered successfully! Proceed to {first_sp.name if first_sp else 'first service'}",
            'services': []
        })
    except Exception as e:
        app.logger.error(f"Registration error: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/service/start/<int:service_id>', methods=['POST'])
def start_service(service_id):
    try:
        service = Service.query.get_or_404(service_id)
        user = db.session.get(User, session['user_id'])
        allowed_districts = get_user_districts(user)
        if service.patient.district_id not in allowed_districts:
            return jsonify({'error': 'Access denied'}), 403

        if service.start_time:
            return jsonify({'error': 'Service already started'}), 400

        patient = service.patient
        service.start_time = get_current_time()

        prev_service = Service.query.filter(
            Service.patient_id == patient.id,
            Service.end_time.isnot(None)
        ).order_by(Service.end_time.desc()).first()

        if prev_service:
            service.waiting_time = (service.start_time - prev_service.end_time).total_seconds()
        else:
            service.waiting_time = (service.start_time - patient.arrival_time).total_seconds()

        db.session.commit()

        return jsonify({
            'id': service.id,
            'service_point': service.service_point.name,
            'start_time': service.start_time.isoformat(),
            'waiting_time': service.waiting_time
        })
    except Exception as e:
        app.logger.error(f"Start service error: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/service/complete/<int:service_id>', methods=['POST'])
def complete_service(service_id):
    try:
        service = Service.query.get_or_404(service_id)
        user = db.session.get(User, session['user_id'])
        allowed_districts = get_user_districts(user)
        if service.patient.district_id not in allowed_districts:
            return jsonify({'error': 'Access denied'}), 403

        if not service.start_time:
            return jsonify({'error': 'Service not started'}), 400
        if service.end_time:
            return jsonify({'status': 'needs_action', 'patient_id': service.patient_id})

        service.end_time = get_current_time()
        service.needs_next_service = True
        db.session.commit()

        return jsonify({
            'status': 'needs_action',
            'patient_id': service.patient_id,
            'message': f'Service at {service.service_point.name} completed',
            'service_time': (service.end_time - service.start_time).total_seconds(),
        })
    except Exception as e:
        app.logger.error(f"Complete service error: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/service/add_manual', methods=['POST'])
def add_manual_service():
    try:
        data = request.get_json()
        patient_id = data.get('patient_id')
        service_point_id = data.get('service_point_id')
        service_name = data.get('service_name')

        if not all([patient_id, service_point_id, service_name]):
            return jsonify({'error': 'Missing required fields'}), 400

        patient = Patient.query.get(patient_id)
        if not patient:
            return jsonify({'error': 'Patient not found'}), 404

        user = db.session.get(User, session['user_id'])
        allowed_districts = get_user_districts(user)
        if patient.district_id not in allowed_districts:
            return jsonify({'error': 'Access denied'}), 403

        service_point = ServicePoint.query.get(service_point_id)
        if not service_point:
            return jsonify({'error': 'Service point not found'}), 404

        if service_point.district_id not in allowed_districts:
            return jsonify({'error': 'Service point not accessible'}), 403

        new_svc = Service(
            patient_id=patient_id,
            service_point_id=service_point_id,
            service_name=service_name
        )
        db.session.add(new_svc)

        for service in patient.services:
            service.needs_next_service = False

        db.session.commit()

        return jsonify({
            'message': 'Service added successfully',
            'service_id': new_svc.id
        })
    except Exception as e:
        app.logger.error(f"Manual service error: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/exit', methods=['POST'])
def exit_patient():
    pid = request.form.get('id')
    if not pid:
        flash('Missing patient ID', 'danger')
        return redirect(url_for('patient_list'))

    try:
        patient = db.session.get(Patient, pid)
        if not patient:
            flash('Patient not found', 'danger')
            return redirect(url_for('patient_list'))

        user = db.session.get(User, session['user_id'])
        allowed_districts = get_user_districts(user)
        if patient.district_id not in allowed_districts:
            flash('Access denied', 'danger')
            return redirect(url_for('dashboard'))

        if patient.exit_time:
            flash('Patient already exited', 'warning')
            return redirect(url_for('patient_detail', id=pid))

        incomplete_services = any(s.end_time is None for s in patient.services)
        if incomplete_services:
            flash('Complete all services before exit', 'danger')
            return redirect(url_for('patient_detail', id=pid))

        patient.exit_time = get_current_time()
        db.session.add(ExitLog(patient_id=pid))

        for service in patient.services:
            service.needs_next_service = False

        db.session.commit()
        flash(f'Patient {patient.name} exited successfully!', 'success')

    except Exception as e:
        app.logger.error(f"Exit patient error: {str(e)}")
        flash('Error exiting patient', 'danger')

    return redirect(url_for('patient_detail', id=pid))

@app.route('/service_points')
@provincial_redirect
def service_points():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    allowed_districts = get_user_districts(user)

    # For admin, allow filtering by district
    if user.role == 'admin':
        district_id = request.args.get('district_id', type=int)
        if district_id:
            # Verify district exists
            district = District.query.get(district_id)
            if district:
                allowed_districts = [district_id]
            else:
                flash('District not found', 'danger')
                # fallback to all districts
        # else keep allowed_districts as all

    sps = ServicePoint.query.filter(ServicePoint.district_id.in_(allowed_districts)).order_by(ServicePoint.order).all()

    for sp in sps:
        sp.queue = Service.query.filter_by(service_point_id=sp.id, start_time=None).all()
        sp.active = Service.query.filter(
            Service.service_point_id == sp.id,
            Service.start_time.isnot(None),
            Service.end_time.is_(None),
            Service.needs_next_service == False
        ).all()
        sp.pending = Service.query.filter(
            Service.service_point_id == sp.id,
            Service.needs_next_service == True
        ).all()

    districts_for_filter = District.query.all() if user.role == 'admin' else []
    selected_district_id = request.args.get('district_id', type=int) if user.role == 'admin' else None

    return render_template('service_points.html',
                           service_points=sps,
                           districts=districts_for_filter,
                           selected_district_id=selected_district_id,
                           user_role=user.role)
@app.route('/service_point/<int:id>')
@provincial_redirect
def service_point_detail(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    allowed_districts = get_user_districts(user)
    sp = ServicePoint.query.get_or_404(id)
    if sp.district_id not in allowed_districts:
        flash('Access denied', 'danger')
        return redirect(url_for('service_points'))

    queue = Service.query.filter_by(service_point_id=id, start_time=None).order_by(Service.id).all()
    active = Service.query.filter(
        Service.service_point_id == id,
        Service.start_time.isnot(None),
        Service.end_time.is_(None),
        Service.needs_next_service == False
    ).order_by(Service.start_time).all()
    pending_actions = Service.query.filter(
        Service.service_point_id == id,
        Service.needs_next_service == True
    ).all()

    all_service_points = ServicePoint.query.filter(ServicePoint.district_id.in_(allowed_districts)).all()

    return render_template(
        'service_point_detail.html',
        service_point=sp,
        queue=queue,
        active=active,
        pending_actions=pending_actions,
        all_service_points=all_service_points
    )

@app.route('/time_metrics')
@provincial_redirect
def time_metrics():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    allowed_districts = get_user_districts(user)

    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    district_id = request.args.get('district_id', type=int)

    query = Patient.query.options(
        joinedload(Patient.services).joinedload(Service.service_point)
    ).filter(Patient.district_id.in_(allowed_districts))

    # Apply district filter if provided and allowed
    if district_id and district_id in allowed_districts:
        query = query.filter(Patient.district_id == district_id)

    if year:
        query = query.filter(extract('year', Patient.arrival_time) == year)
    if month:
        query = query.filter(extract('month', Patient.arrival_time) == month)

    patients = query.all()
    now = get_current_time()

    for patient in patients:
        patient.total_wait = 0
        for service in patient.services:
            service.service_time = get_service_time(service)
            if service.waiting_time:
                patient.total_wait += service.waiting_time

        if patient.exit_time:
            patient.total_time = (patient.exit_time - patient.arrival_time).total_seconds()
        else:
            patient.total_time = (now - patient.arrival_time).total_seconds()

    # For filter dropdowns (admin can see all districts, others see their own)
    available_districts = []
    if user.role == 'admin':
        available_districts = District.query.all()
    elif user.role in ['district_admin', 'reception', 'doctor', 'nurse', 'pharmacy', 'rehabilitation', 'nutrition']:
        available_districts = [District.query.get(user.district_id)] if user.district_id else []

    return render_template('time_metrics.html', patients=patients, filter_month=month, filter_year=year,
                           filter_district=district_id, available_districts=available_districts)

@app.route('/time_analysis')
def time_analysis():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    district_id = request.args.get('district_id', type=int)
    province_id = request.args.get('province_id', type=int)
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    # Determine allowed districts
    if user.role == 'provincial':
        # Provincial: allow only if a district in their province is selected
        if district_id:
            district = District.query.get(district_id)
            if not district or district.province_id != user.province_id:
                flash('Access denied', 'danger')
                return redirect(url_for('provincial_dashboard'))
            allowed_districts = [district_id]
        else:
            # No district selected, redirect to provincial dashboard
            return redirect(url_for('provincial_dashboard'))
    elif user.role == 'admin':
        # Admin can filter by district or province, else all
        if district_id:
            allowed_districts = [district_id]
        elif province_id:
            districts_in_province = District.query.filter_by(province_id=province_id).all()
            allowed_districts = [d.id for d in districts_in_province]
        else:
            allowed_districts = [d.id for d in District.query.all()]
    else:
        # Other roles (district_admin, reception, etc.) use existing scope
        allowed_districts = get_user_districts(user)

    query = Patient.query.filter(Patient.district_id.in_(allowed_districts))

    if year:
        query = query.filter(extract('year', Patient.arrival_time) == year)
    if month:
        query = query.filter(extract('month', Patient.arrival_time) == month)

    patients = query.all()

    # Metrics
    total_patients = len(patients)
    waiting_times = []
    service_times = []
    over_2_hours = 0
    completed_patients = []

    for p in patients:
        total_wait = 0
        for service in p.services:
            if service.waiting_time:
                total_wait += service.waiting_time
                waiting_times.append(service.waiting_time)
            if service.start_time and service.end_time:
                service_times.append((service.end_time - service.start_time).total_seconds())
        if p.exit_time:
            total_seconds = (p.exit_time - p.arrival_time).total_seconds()
            completed_patients.append(p)
            if total_seconds > 2 * 3600:
                over_2_hours += 1

    # Patients with multiple services
    patients_multiple_services = sum(1 for p in patients if len(p.services) > 1)

    # Service point metrics (only those in allowed districts)
    sp_metrics = []
    allowed_sp = ServicePoint.query.filter(ServicePoint.district_id.in_(allowed_districts)).all()
    for sp in allowed_sp:
        sp_services = Service.query.filter_by(service_point_id=sp.id).all()
        wait_times = [s.waiting_time for s in sp_services if s.waiting_time]
        if wait_times:
            over_30 = len([t for t in wait_times if t > 1800])
            mean_time = mean(wait_times)
            median_time = median(wait_times)
            try:
                mode_time = mode(wait_times)
            except:
                mode_time = mean_time
            sp_metrics.append({
                'name': sp.name,
                'over_30': over_30,
                'mean': mean_time,
                'median': median_time,
                'mode': mode_time
            })
        else:
            sp_metrics.append({
                'name': sp.name,
                'over_30': 0,
                'mean': 0,
                'median': 0,
                'mode': 0
            })

    # Poor performers
    poor_performers = []
    total_services = len([s for p in patients for s in p.services])
    if total_services > 0:
        for sp in sp_metrics:
            if sp['over_30'] > total_services * 0.5:
                poor_performers.append(sp)

    # Peak hour
    hour_counts = Counter()
    for p in patients:
        hour = p.arrival_time.hour
        hour_counts[hour] += 1
    peak_hour, peak_count = hour_counts.most_common(1)[0] if hour_counts else (0, 0)

    # Throughput
    if completed_patients:
        total_time = sum((p.exit_time - p.arrival_time).total_seconds() for p in completed_patients)
        throughput = len(completed_patients) / (total_time / 3600) if total_time > 0 else 0
    else:
        throughput = 0

    avg_stay = total_time / len(completed_patients) if completed_patients else 0
    overall_avg_wait = mean(waiting_times) if waiting_times else 0
    longest_wait = max(waiting_times) if waiting_times else 0
    shortest_wait = min(waiting_times) if waiting_times else 0
    total_service_time = sum(service_times) if service_times else 0

    # For filter dropdowns (admin and provincial only)
    available_districts = []
    available_provinces = []
    if user.role == 'admin':
        available_districts = District.query.all()
        available_provinces = Province.query.all()
    elif user.role == 'provincial':
        # Only the districts in their province
        available_districts = District.query.filter_by(province_id=user.province_id).all()
        available_provinces = [Province.query.get(user.province_id)] if user.province_id else []
    # Others don't get filters (they only see their own district)

    return render_template(
        'time_analysis.html',
        total_patients=total_patients,
        longest_wait=longest_wait,
        shortest_wait=shortest_wait,
        total_service_time=total_service_time,
        over_2_hours=over_2_hours,
        patients_multiple_services=patients_multiple_services,
        sp_metrics=sp_metrics,
        poor_performers=poor_performers,
        peak_hour=peak_hour,
        peak_count=peak_count,
        throughput=throughput,
        avg_stay=avg_stay,
        overall_avg_wait=overall_avg_wait,
        filter_month=month,
        filter_year=year,
        filter_district=district_id,
        filter_province=province_id,
        available_districts=available_districts,
        available_provinces=available_provinces,
        user_role=user.role
    )

@app.route('/survey', methods=['GET', 'POST'])
def survey():
    if request.method == 'POST':
        # Get district_id from form
        district_id = request.form.get('district_id')
        if not district_id:
            # For logged-in district staff, use their district from session
            if session.get('role') in ['district_admin', 'reception']:
                district_id = session.get('district_id')
            if not district_id:
                flash('Please select a district', 'danger')
                return redirect(url_for('survey'))

        s = Survey(
            patient_id=None,
            district_id=district_id,
            satisfaction=(request.form['satisfaction'] == 'yes'),
            received_services=(request.form['received_services'] == 'yes'),
            courteous=(request.form['courteous'] == 'yes'),
            speed=(request.form['speed'] == 'yes'),
            comments=request.form.get('comments', '').strip()
        )
        db.session.add(s)
        db.session.commit()
        flash('Survey submitted successfully. Thank you for your feedback!', 'success')
        return redirect(url_for('survey'))

    # GET: load districts for dropdown (if user not logged in as district staff)
    districts = []
    if 'user_id' not in session or session.get('role') not in ['district_admin', 'reception']:
        districts = District.query.all()
    return render_template('survey.html', districts=districts)

@app.route('/survey/analysis')
def survey_analysis():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    district_id = request.args.get('district_id', type=int)
    province_id = request.args.get('province_id', type=int)
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    from sqlalchemy import or_

    # Build base query
    if user.role == 'provincial':
        if district_id:
            district = District.query.get(district_id)
            if not district or district.province_id != user.province_id:
                flash('Access denied', 'danger')
                return redirect(url_for('provincial_dashboard'))
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id == district_id),
                    Survey.district_id == district_id
                )
            )
        else:
            districts_in_province = District.query.filter_by(province_id=user.province_id).all()
            district_ids = [d.id for d in districts_in_province]
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id.in_(district_ids)),
                    Survey.district_id.in_(district_ids)
                )
            )
    elif user.role == 'admin':
        if district_id:
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id == district_id),
                    Survey.district_id == district_id
                )
            )
        elif province_id:
            districts_in_province = District.query.filter_by(province_id=province_id).all()
            district_ids = [d.id for d in districts_in_province]
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id.in_(district_ids)),
                    Survey.district_id.in_(district_ids)
                )
            )
        else:
            # All surveys that have a district (either via patient or direct)
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id.isnot(None)),
                    Survey.district_id.isnot(None)
                )
            )
    else:
        # district_admin, reception, etc.
        allowed_districts = get_user_districts(user)
        if allowed_districts:
            query = Survey.query.filter(
                or_(
                    Survey.patient.has(Patient.district_id.in_(allowed_districts)),
                    Survey.district_id.in_(allowed_districts)
                )
            )
        else:
            query = Survey.query.filter(False)

    # Apply date filters
    if year:
        query = query.filter(extract('year', Survey.timestamp) == year)
    if month:
        query = query.filter(extract('month', Survey.timestamp) == month)

    surveys = query.all()
    total_surveys = len(surveys)

    # Compute counts
    satisfaction_yes = sum(1 for s in surveys if s.satisfaction)
    services_yes = sum(1 for s in surveys if s.received_services)
    courteous_yes = sum(1 for s in surveys if s.courteous)
    speed_yes = sum(1 for s in surveys if s.speed)

    kpis = {
        'satisfaction': (satisfaction_yes / total_surveys * 100) if total_surveys else 0,
        'received_services': (services_yes / total_surveys * 100) if total_surveys else 0,
        'courteous': (courteous_yes / total_surveys * 100) if total_surveys else 0,
        'speed': (speed_yes / total_surveys * 100) if total_surveys else 0,
    }

    stats = {
        'satisfaction': {'yes': satisfaction_yes, 'no': total_surveys - satisfaction_yes},
        'received_services': {'yes': services_yes, 'no': total_surveys - services_yes},
        'courteous': {'yes': courteous_yes, 'no': total_surveys - courteous_yes},
        'speed': {'yes': speed_yes, 'no': total_surveys - speed_yes},
    }

    # Score distribution (optional)
    score_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for s in surveys:
        score = sum([s.satisfaction, s.received_services, s.courteous, s.speed])
        if 1 <= score <= 4:
            score_counts[score] += 1

    # Comment categorization
    keywords = {
        'quality': ['good', 'bad', 'excellent', 'poor', 'clean'],
        'accessibility': ['medicine', 'service', 'access', 'lack'],
        'courtesy': ['rude', 'friendly', 'courteous', 'polite'],
        'speed': ['wait', 'slow', 'time', 'delay']
    }
    comment_counts = Counter()
    for s in surveys:
        txt = (s.comments or '').lower()
        for cat, words in keywords.items():
            if any(w in txt for w in words):
                comment_counts[cat] += 1

    comments = []
    for s in surveys:
        if s.comments and s.comments.strip():
            comments.append({
                'comment': s.comments,
                'timestamp': s.timestamp.strftime('%Y-%m-%d %H:%M')
            })

    # Filter dropdowns for UI
    available_districts = []
    available_provinces = []
    if user.role == 'admin':
        available_districts = District.query.all()
        available_provinces = Province.query.all()
    elif user.role == 'provincial':
        available_districts = District.query.filter_by(province_id=user.province_id).all()
        available_provinces = [Province.query.get(user.province_id)] if user.province_id else []
    # Others don't get filters

    return render_template(
        'survey_analysis.html',
        stats=stats,
        kpis=kpis,
        comment_counts=comment_counts,
        score_counts=score_counts,
        total_surveys=total_surveys,
        comments=comments,
        filter_month=month,
        filter_year=year,
        filter_district=district_id,
        filter_province=province_id,
        available_districts=available_districts,
        available_provinces=available_provinces,
        user_role=user.role
    )

@app.route('/comments_review')
@provincial_redirect
def comments_review():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    surveys = Survey.query.order_by(Survey.timestamp.desc()).all()
    grouped = defaultdict(list)
    for s in surveys:
        txt = (s.comments or '').strip().lower()
        if txt:
            grouped[txt].append(s.timestamp.strftime('%Y-%m-%d %H:%M'))

    grouped_list = [{'comment': k, 'count': len(v), 'timestamps': v} for k, v in grouped.items()]
    return render_template('comments_review.html', grouped=grouped_list, raw=surveys)

# Provincial Dashboard
@app.route('/provincial')
def provincial_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role != 'provincial':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    # Get districts in this province
    districts_in_province = District.query.filter_by(province_id=user.province_id).all()
    district_ids = [d.id for d in districts_in_province]

    # Province-wide KPIs
    # 1. Patients
    total_patients = Patient.query.filter(Patient.district_id.in_(district_ids)).count()

    # 2. Wait times
    wait_times = []
    patients = Patient.query.filter(Patient.district_id.in_(district_ids)).all()
    for p in patients:
        for s in p.services:
            if s.waiting_time:
                wait_times.append(s.waiting_time)
    avg_wait = mean(wait_times) if wait_times else 0
    shortest_wait = min(wait_times) if wait_times else 0
    longest_wait = max(wait_times) if wait_times else 0

    # 3. Survey KPIs (include both patient-linked and direct district-linked surveys)
    surveys = Survey.query.filter(
        or_(
            Survey.patient.has(Patient.district_id.in_(district_ids)),
            Survey.district_id.in_(district_ids)
        )
    ).all()
    total_surveys = len(surveys)
    satisfaction_count = sum(1 for s in surveys if s.satisfaction)
    services_received_count = sum(1 for s in surveys if s.received_services)
    courteous_count = sum(1 for s in surveys if s.courteous)
    speed_count = sum(1 for s in surveys if s.speed)

    satisfaction_rate = (satisfaction_count / total_surveys * 100) if total_surveys else 0
    services_received_rate = (services_received_count / total_surveys * 100) if total_surveys else 0
    courtesy_rate = (courteous_count / total_surveys * 100) if total_surveys else 0
    speed_rate = (speed_count / total_surveys * 100) if total_surveys else 0

    # Build district stats (for the cards)
    district_stats = []
    for d in districts_in_province:
        patient_count = Patient.query.filter_by(district_id=d.id).count()

        # Average wait for this district
        d_wait_times = []
        for p in Patient.query.filter_by(district_id=d.id).all():
            for s in p.services:
                if s.waiting_time:
                    d_wait_times.append(s.waiting_time)
        d_avg_wait = mean(d_wait_times) if d_wait_times else 0

        # Satisfaction rate for this district (surveys linked either by patient district or direct district_id)
        d_surveys = Survey.query.filter(
            or_(
                Survey.patient.has(Patient.district_id == d.id),
                Survey.district_id == d.id
            )
        ).all()
        d_sat = (sum(1 for s in d_surveys if s.satisfaction) / len(d_surveys) * 100) if d_surveys else 0

        district_stats.append({
            'id': d.id,
            'name': d.name,
            'patients': patient_count,
            'avg_wait': d_avg_wait,
            'satisfaction': d_sat
        })

    return render_template(
        'provincial_dashboard.html',
        province_name=user.province.name if user.province else 'Unknown',
        total_patients=total_patients,
        avg_wait=avg_wait,
        shortest_wait=shortest_wait,
        longest_wait=longest_wait,
        satisfaction_rate=satisfaction_rate,
        services_received_rate=services_received_rate,
        courtesy_rate=courtesy_rate,
        speed_rate=speed_rate,
        district_stats=district_stats
    )

# Patient registration web
@app.route('/register_patient', methods=['GET', 'POST'])
@provincial_redirect
def register_patient_web():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin', 'reception']:
        flash('You do not have permission to register patients', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name = request.form.get('name')
        if not name or len(name.strip()) < 2:
            flash('Please enter a valid patient name', 'danger')
            return render_template('register_patient.html', districts=districts_for_form, user=user)

        if user.role == 'admin':
            district_id = request.form.get('district_id')
            if not district_id:
                flash('Please select a district', 'danger')
                return render_template('register_patient.html', districts=District.query.all(), user=user)
        else:
            if user.district_id is None:
                flash('User not assigned to a district', 'danger')
                return redirect(url_for('dashboard'))
            district_id = user.district_id

        try:
            patient = Patient(name=name.strip(), arrival_time=get_current_time(), district_id=district_id)
            db.session.add(patient)
            db.session.commit()

            first_sp = ServicePoint.query.filter_by(order=1, district_id=district_id).first()
            if first_sp:
                svc = Service(
                    patient_id=patient.id,
                    service_point_id=first_sp.id,
                    service_name=first_sp.default_service
                )
                db.session.add(svc)
                db.session.commit()

            flash(f'Patient {name} registered successfully!', 'success')
            return redirect(url_for('patient_detail', id=patient.id))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Registration error: {str(e)}")
            flash('Error registering patient. Please try again.', 'danger')

    districts_for_form = District.query.all() if user.role == 'admin' else []
    return render_template('register_patient.html', districts=districts_for_form, user=user)

# User management (for admin and district admin)
@app.route('/admin/users')
def manage_users():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if user.role == 'admin':
        users = User.query.all()
    else:
        users = User.query.filter_by(district_id=user.district_id).all()
    return render_template('manage_users.html', users=users)

@app.route('/admin/user/add', methods=['GET', 'POST'])
def add_user():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_user = db.session.get(User, session['user_id'])
    if current_user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        district_id = request.form.get('district_id')
        province_id = request.form.get('province_id')

        if not all([username, password, role]):
            flash('All fields are required', 'danger')
            return redirect(url_for('add_user'))

        if current_user.role != 'admin':
            if role in ['admin', 'provincial', 'national']:
                flash('You cannot create users with this role', 'danger')
                return redirect(url_for('add_user'))
            district_id = current_user.district_id
        else:
            if role in ['district_admin', 'reception', 'doctor', 'nurse', 'pharmacy', 'rehabilitation', 'nutrition']:
                if not district_id:
                    flash('District is required for this role', 'danger')
                    return redirect(url_for('add_user'))
            if role == 'provincial':
                if not province_id:
                    flash('Province is required for provincial role', 'danger')
                    return redirect(url_for('add_user'))

        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('add_user'))

        user = User(username=username, role=role)
        user.set_password(password)
        if district_id:
            user.district_id = int(district_id)
        if province_id:
            user.province_id = int(province_id)
        db.session.add(user)
        db.session.commit()

        flash('User created successfully', 'success')
        return redirect(url_for('manage_users'))

    districts = District.query.all()
    provinces = Province.query.all()
    if current_user.role != 'admin':
        districts = [d for d in districts if d.id == current_user.district_id]
    return render_template('edit_user.html', user=None, districts=districts, provinces=provinces)

@app.route('/admin/user/edit/<int:id>', methods=['GET', 'POST'])
def edit_user(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, id)
    current_user = db.session.get(User, session['user_id'])
    if current_user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if current_user.role != 'admin' and user.district_id != current_user.district_id:
        flash('You cannot edit users from other districts', 'danger')
        return redirect(url_for('manage_users'))

    if request.method == 'POST':
        user.role = request.form.get('role')
        if current_user.role == 'admin':
            user.district_id = request.form.get('district_id') or None
            user.province_id = request.form.get('province_id') or None
        else:
            user.district_id = current_user.district_id
        password = request.form.get('password')
        if password:
            user.set_password(password)
        db.session.commit()
        flash('User updated successfully', 'success')
        return redirect(url_for('manage_users'))

    districts = District.query.all()
    provinces = Province.query.all()
    if current_user.role != 'admin':
        districts = [d for d in districts if d.id == current_user.district_id]
    return render_template('edit_user.html', user=user, districts=districts, provinces=provinces)

@app.route('/admin/user/delete/<int:id>', methods=['POST'])
def delete_user(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, id)
    current_user = db.session.get(User, session['user_id'])
    if current_user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if current_user.role != 'admin' and user.district_id != current_user.district_id:
        flash('You cannot delete users from other districts', 'danger')
        return redirect(url_for('manage_users'))
    if user.role == 'admin':
        flash('Cannot delete admin user', 'danger')
    else:
        db.session.delete(user)
        db.session.commit()
        flash('User deleted successfully', 'success')
    return redirect(url_for('manage_users'))

# Service point management (for admin and district admin)
@app.route('/admin/service_points')
def manage_service_points():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    # Get district filter from query string (only for admin)
    district_id = request.args.get('district_id', type=int)

    if user.role == 'admin':
        if district_id:
            points = ServicePoint.query.filter_by(district_id=district_id).order_by(ServicePoint.order).all()
        else:
            points = ServicePoint.query.order_by(ServicePoint.order).all()
        # All districts for the dropdown
        districts = District.query.all()
        selected_district_id = district_id
    else:
        # district_admin: only their own district
        points = ServicePoint.query.filter_by(district_id=user.district_id).order_by(ServicePoint.order).all()
        districts = []  # no filter needed
        selected_district_id = user.district_id

    return render_template('manage_service_points.html',
                           points=points,
                           districts=districts,
                           selected_district_id=selected_district_id,
                           user_role=user.role)
@app.route('/admin/service_point/add', methods=['GET', 'POST'])
def add_service_point():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name = request.form.get('name')
        order = request.form.get('order')
        default_service = request.form.get('default_service')
        district_id = request.form.get('district_id')

        if not all([name, order, default_service]):
            flash('All fields are required', 'danger')
            return redirect(url_for('add_service_point'))

        if user.role == 'admin' and not district_id:
            flash('District is required for admin', 'danger')
            return redirect(url_for('add_service_point'))

        try:
            order = int(order)
        except ValueError:
            flash('Order must be a number', 'danger')
            return redirect(url_for('add_service_point'))

        if user.role != 'admin':
            district_id = user.district_id

        if ServicePoint.query.filter_by(name=name, district_id=district_id).first():
            flash('Service point name already exists in this district', 'danger')
            return redirect(url_for('add_service_point'))

        sp = ServicePoint(
            name=name,
            order=order,
            default_service=default_service,
            district_id=district_id
        )
        db.session.add(sp)
        db.session.commit()
        flash('Service point created successfully', 'success')
        return redirect(url_for('manage_service_points'))

    districts = []
    if user.role == 'admin':
        districts = District.query.all()
    return render_template('edit_service_point.html', point=None, districts=districts, user_role=user.role)

@app.route('/admin/service_point/edit/<int:id>', methods=['GET', 'POST'])
def edit_service_point(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    point = ServicePoint.query.get_or_404(id)
    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if user.role != 'admin' and (user.district_id != point.district_id):
        flash('You do not have permission to edit this service point', 'danger')
        return redirect(url_for('manage_service_points'))

    if request.method == 'POST':
        point.name = request.form.get('name')
        point.order = int(request.form.get('order'))
        point.default_service = request.form.get('default_service')
        if user.role == 'admin':
            point.district_id = request.form.get('district_id')
        db.session.commit()
        flash('Service point updated successfully', 'success')
        return redirect(url_for('manage_service_points'))

    districts = []
    if user.role == 'admin':
        districts = District.query.all()
    return render_template('edit_service_point.html', point=point, districts=districts, user_role=user.role)

@app.route('/admin/service_point/delete/<int:id>', methods=['POST'])
def delete_service_point(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    point = ServicePoint.query.get_or_404(id)
    user = db.session.get(User, session['user_id'])
    if user.role not in ['admin', 'district_admin']:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    if user.role != 'admin' and (user.district_id != point.district_id):
        flash('You do not have permission to delete this service point', 'danger')
        return redirect(url_for('manage_service_points'))

    if Service.query.filter_by(service_point_id=id).count() > 0:
        flash('Cannot delete service point with associated services', 'danger')
    else:
        db.session.delete(point)
        db.session.commit()
        flash('Service point deleted successfully', 'success')
    return redirect(url_for('manage_service_points'))

# Admin-only actions
@app.route('/admin/clear_data', methods=['GET', 'POST'])
@admin_required
def clear_data():
    if request.method == 'POST':
        if request.form.get('confirmation') == 'DELETE':
            db.session.query(Service).delete()
            db.session.query(Survey).delete()
            db.session.query(ExitLog).delete()
            db.session.query(Patient).delete()
            db.session.commit()
            flash('All patient data cleared successfully', 'success')
        else:
            flash('Confirmation failed', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('clear_data.html')

@app.route('/admin/backup', methods=['GET'])
@admin_required
def create_backup():
    try:
        backup_dir = os.path.join(app.root_path, 'backups')
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = os.path.join(backup_dir, f'meditrack_backup_{timestamp}.db')
        src_db = os.path.join(app.root_path, 'patients.db')
        shutil.copyfile(src_db, backup_file)
        flash(f'Backup created successfully: {backup_file}', 'success')
    except Exception as e:
        app.logger.error(f"Backup failed: {str(e)}")
        flash(f'Backup creation failed: {str(e)}', 'danger')
    return redirect(url_for('manage_service_points'))

@app.route('/admin/restore', methods=['POST'])
@admin_required
def restore_backup():
    if 'backup_file' not in request.files:
        flash('No file selected', 'danger')
        return redirect(url_for('manage_service_points'))
    file = request.files['backup_file']
    if file.filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('manage_service_points'))
    if not file.filename.endswith('.db'):
        flash('Invalid file type. Please upload a .db file', 'danger')
        return redirect(url_for('manage_service_points'))

    try:
        db.session.remove()
        temp_path = os.path.join(app.root_path, 'temp_restore.db')
        file.save(temp_path)
        db_path = os.path.join(app.root_path, 'patients.db')
        shutil.copyfile(temp_path, db_path)
        os.remove(temp_path)
        flash('Database restored successfully. Please log in again.', 'success')
    except Exception as e:
        app.logger.error(f"Restore failed: {str(e)}")
        flash(f'Restore failed: {str(e)}', 'danger')
    return redirect(url_for('manage_service_points'))

# District and province management (admin only)
@app.route('/admin/districts')
@admin_required
def manage_districts():
    districts = District.query.all()
    return render_template('manage_districts.html', districts=districts)

@app.route('/admin/district/add', methods=['GET', 'POST'])
@admin_required
def add_district():
    if request.method == 'POST':
        name = request.form.get('name')
        province_id = request.form.get('province_id')
        if not name:
            flash('District name required', 'danger')
            return redirect(url_for('add_district'))
        district = District(name=name, province_id=province_id if province_id else None)
        db.session.add(district)
        db.session.commit()
        flash(f'District {name} created', 'success')
        return redirect(url_for('manage_districts'))
    provinces = Province.query.all()
    return render_template('edit_district.html', district=None, provinces=provinces)

@app.route('/admin/district/edit/<int:id>', methods=['GET', 'POST'])
@admin_required
def edit_district(id):
    district = District.query.get_or_404(id)
    if request.method == 'POST':
        district.name = request.form.get('name')
        district.province_id = request.form.get('province_id') or None
        db.session.commit()
        flash('District updated', 'success')
        return redirect(url_for('manage_districts'))
    provinces = Province.query.all()
    return render_template('edit_district.html', district=district, provinces=provinces)

@app.route('/admin/district/delete/<int:id>', methods=['POST'])
@admin_required
def delete_district(id):
    district = District.query.get_or_404(id)
    if Patient.query.filter_by(district_id=id).count() > 0:
        flash('Cannot delete district with associated patients', 'danger')
    elif ServicePoint.query.filter_by(district_id=id).count() > 0:
        flash('Cannot delete district with associated service points', 'danger')
    elif User.query.filter_by(district_id=id).count() > 0:
        flash('Cannot delete district with associated users', 'danger')
    else:
        db.session.delete(district)
        db.session.commit()
        flash('District deleted', 'success')
    return redirect(url_for('manage_districts'))

@app.route('/admin/provinces')
@admin_required
def manage_provinces():
    provinces = Province.query.all()
    return render_template('manage_provinces.html', provinces=provinces)

@app.route('/admin/province/add', methods=['GET', 'POST'])
@admin_required
def add_province():
    if request.method == 'POST':
        name = request.form.get('name')
        if not name:
            flash('Province name required', 'danger')
            return redirect(url_for('add_province'))
        province = Province(name=name)
        db.session.add(province)
        db.session.commit()
        flash(f'Province {name} created', 'success')
        return redirect(url_for('manage_provinces'))
    return render_template('edit_province.html', province=None)

@app.route('/admin/province/edit/<int:id>', methods=['GET', 'POST'])
@admin_required
def edit_province(id):
    province = Province.query.get_or_404(id)
    if request.method == 'POST':
        province.name = request.form.get('name')
        db.session.commit()
        flash('Province updated', 'success')
        return redirect(url_for('manage_provinces'))
    return render_template('edit_province.html', province=province)

@app.route('/admin/province/delete/<int:id>', methods=['POST'])
@admin_required
def delete_province(id):
    province = Province.query.get_or_404(id)
    if District.query.filter_by(province_id=id).count() > 0:
        flash('Cannot delete province with associated districts', 'danger')
    else:
        db.session.delete(province)
        db.session.commit()
        flash('Province deleted', 'success')
    return redirect(url_for('manage_provinces'))

@app.route('/provincial/district/<int:district_id>')
def provincial_district_analysis(district_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role != 'provincial':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    district = District.query.get_or_404(district_id)
    if district.province_id != user.province_id:
        flash('You do not have access to this district', 'danger')
        return redirect(url_for('provincial_dashboard'))

    # Get filter parameters
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    # Time analysis data
    patients = Patient.query.filter_by(district_id=district_id)
    if year:
        patients = patients.filter(extract('year', Patient.arrival_time) == year)
    if month:
        patients = patients.filter(extract('month', Patient.arrival_time) == month)
    patients = patients.all()
    total_patients = len(patients)

    waiting_times = []
    service_times = []
    over_2_hours = 0
    completed_patients = []
    for p in patients:
        for s in p.services:
            if s.waiting_time:
                waiting_times.append(s.waiting_time)
            if s.start_time and s.end_time:
                service_times.append((s.end_time - s.start_time).total_seconds())
        if p.exit_time:
            total_seconds = (p.exit_time - p.arrival_time).total_seconds()
            completed_patients.append(p)
            if total_seconds > 2 * 3600:
                over_2_hours += 1

    patients_multiple_services = sum(1 for p in patients if len(p.services) > 1)

    # Throughput
    if completed_patients:
        total_time = sum((p.exit_time - p.arrival_time).total_seconds() for p in completed_patients)
        throughput = len(completed_patients) / (total_time / 3600) if total_time > 0 else 0
    else:
        throughput = 0

    avg_stay = total_time / len(completed_patients) if completed_patients else 0
    overall_avg_wait = mean(waiting_times) if waiting_times else 0
    longest_wait = max(waiting_times) if waiting_times else 0
    shortest_wait = min(waiting_times) if waiting_times else 0
    total_service_time = sum(service_times) if service_times else 0

    time_data = {
        'total_patients': total_patients,
        'longest_wait': longest_wait,
        'shortest_wait': shortest_wait,
        'over_2_hours': over_2_hours,
        'patients_multiple_services': patients_multiple_services,
        'throughput': throughput,
        'avg_stay': avg_stay,
        'overall_avg_wait': overall_avg_wait,
        'total_service_time': total_service_time,
    }

    # Survey analysis data
    surveys = Survey.query.filter(Survey.patient.has(Patient.district_id == district_id))
    if year:
        surveys = surveys.filter(extract('year', Survey.timestamp) == year)
    if month:
        surveys = surveys.filter(extract('month', Survey.timestamp) == month)
    surveys = surveys.all()
    total_surveys = len(surveys)
    satisfaction_yes = sum(1 for s in surveys if s.satisfaction)
    satisfaction_rate = (satisfaction_yes / total_surveys * 100) if total_surveys else 0
    received_yes = sum(1 for s in surveys if s.received_services)
    received_rate = (received_yes / total_surveys * 100) if total_surveys else 0
    courteous_yes = sum(1 for s in surveys if s.courteous)
    courteous_rate = (courteous_yes / total_surveys * 100) if total_surveys else 0
    speed_yes = sum(1 for s in surveys if s.speed)
    speed_rate = (speed_yes / total_surveys * 100) if total_surveys else 0

    survey_data = {
        'total_surveys': total_surveys,
        'satisfaction_yes': satisfaction_yes,
        'satisfaction_rate': satisfaction_rate,
        'received_yes': received_yes,
        'received_rate': received_rate,
        'courteous_yes': courteous_yes,
        'courteous_rate': courteous_rate,
        'speed_yes': speed_yes,
        'speed_rate': speed_rate,
    }

    return render_template('provincial_district_analysis.html',
                           district=district,
                           time_data=time_data,
                           survey_data=survey_data,
                           filter_year=year,
                           filter_month=month)

@app.route('/admin/patient/delete/<int:id>', methods=['POST'])
@admin_required
def delete_patient(id):
    patient = Patient.query.get_or_404(id)
    # Delete associated services, surveys, exit logs will cascade due to relationships
    db.session.delete(patient)
    db.session.commit()
    flash(f'Patient {patient.name} has been deleted.', 'success')
    return redirect(url_for('patient_list'))

# CLI command to initialize database
@app.cli.command("init")
def init_db():
    """Initialize the database with default data"""
    db.create_all()

    default_province = Province.query.filter_by(name='Default').first()
    if not default_province:
        default_province = Province(name='Default')
        db.session.add(default_province)
        db.session.commit()

    default_district = District.query.filter_by(name='Default').first()
    if not default_district:
        default_district = District(name='Default', province_id=default_province.id)
        db.session.add(default_district)
        db.session.commit()

    Patient.query.filter(Patient.district_id.is_(None)).update({Patient.district_id: default_district.id})
    ServicePoint.query.filter(ServicePoint.district_id.is_(None)).update({ServicePoint.district_id: default_district.id})
    User.query.filter(User.district_id.is_(None)).update({User.district_id: default_district.id})
    db.session.commit()

    users = [
        {'username': 'admin', 'password': 'admin123', 'role': 'admin'},
        {'username': 'reception', 'password': 'reception123', 'role': 'reception', 'district_id': default_district.id},
        {'username': 'doctor', 'password': 'doctor123', 'role': 'doctor', 'district_id': default_district.id},
        {'username': 'nurse', 'password': 'nurse123', 'role': 'nurse', 'district_id': default_district.id},
        {'username': 'pharmacy', 'password': 'pharmacy123', 'role': 'pharmacy', 'district_id': default_district.id},
        {'username': 'rehabilitation', 'password': 'rehab123', 'role': 'rehabilitation', 'district_id': default_district.id},
        {'username': 'nutrition', 'password': 'nutrition123', 'role': 'nutrition', 'district_id': default_district.id},
    ]
    for u in users:
        if not User.query.filter_by(username=u['username']).first():
            usr = User(username=u['username'], role=u['role'], district_id=u.get('district_id'))
            usr.set_password(u['password'])
            db.session.add(usr)

    sp_data = [
        {'name': 'Registration', 'order': 1, 'default_service': 'Check-in'},
        {'name': 'Reception', 'order': 2, 'default_service': 'Document Verification'},
        {'name': 'Outpatient Department', 'order': 3, 'default_service': 'Doctor Consultation'},
        {'name': 'Family Child Health', 'order': 4, 'default_service': 'Child Checkup'},
        {'name': 'Opportunistic Infection', 'order': 5, 'default_service': 'OI Screening'},
        {'name': 'Pharmacy', 'order': 6, 'default_service': 'Medication Dispensing'},
        {'name': 'Laboratory', 'order': 7, 'default_service': 'Lab Tests'},
        {'name': 'Rehabilitation', 'order': 8, 'default_service': 'Therapy Session'},
        {'name': 'Nutrition', 'order': 9, 'default_service': 'Diet Consultation'},
    ]
    for d in sp_data:
        if not ServicePoint.query.filter_by(name=d['name'], district_id=default_district.id).first():
            sp = ServicePoint(name=d['name'], order=d['order'], default_service=d['default_service'], district_id=default_district.id)
            db.session.add(sp)

    db.session.commit()
    print("Initialized the database with default users, districts, and service points.")

if __name__ == '__main__':
    app.logger.info('Starting MediTrack application')
    app.run(debug=True, host='0.0.0.0', port=5004)