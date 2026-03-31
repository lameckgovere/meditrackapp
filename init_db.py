from app import app
from database import db
from models import Province, District, ServicePoint, User

with app.app_context():
    db.create_all()
    # Create default province and district if not exist
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

    # Update existing records without district
    # (skip if you have no existing data)
    # ...

    # Create default users
    users = [
        {'username': 'admin', 'password': 'admin123', 'role': 'admin'},
        {'username': 'reception', 'password': 'reception123', 'role': 'reception', 'district_id': default_district.id},
        # ... other users
    ]
    for u in users:
        if not User.query.filter_by(username=u['username']).first():
            usr = User(username=u['username'], role=u['role'], district_id=u.get('district_id'))
            usr.set_password(u['password'])
            db.session.add(usr)

    # Create default service points for default district
    sp_data = [
        {'name': 'Registration', 'order': 1, 'default_service': 'Check-in'},
        # ... other service points
    ]
    for d in sp_data:
        if not ServicePoint.query.filter_by(name=d['name'], district_id=default_district.id).first():
            sp = ServicePoint(name=d['name'], order=d['order'], default_service=d['default_service'], district_id=default_district.id)
            db.session.add(sp)

    db.session.commit()
    print("Database initialized.")