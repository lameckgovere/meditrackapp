from app import app
from database import db
from models import Province, District, User

with app.app_context():
    # Create Midlands province
    midlands = Province.query.filter_by(name='Midlands').first()
    if not midlands:
        midlands = Province(name='Midlands')
        db.session.add(midlands)
        db.session.commit()
        print("Created Midlands province")
    else:
        print("Midlands province already exists")

    # List of 8 districts
    districts = ["Gweru", "Kwekwe", "Gokwe North", "Gokwe South",
                 "Chirumanzu", "Shurugwi", "Zvishavane", "Mberengwa"]

    for d in districts:
        district = District.query.filter_by(name=d).first()
        if not district:
            district = District(name=d, province_id=midlands.id)
            db.session.add(district)
            print(f"Added district: {d}")

    db.session.commit()
    print("Districts added.")

    # Optionally create a provincial user for Midlands
    prov_user = User.query.filter_by(username='midlands_admin').first()
    if not prov_user:
        prov_user = User(username='midlands_admin', role='provincial', province_id=midlands.id)
        prov_user.set_password('midlands123')
        db.session.add(prov_user)
        print("Created provincial user: midlands_admin / midlands123")

    # Optionally create a district admin for each district
    for d in districts:
        district_obj = District.query.filter_by(name=d).first()
        username = f"{d.lower()}_admin"
        if not User.query.filter_by(username=username).first():
            admin_user = User(username=username, role='district_admin', district_id=district_obj.id)
            admin_user.set_password(f"{d.lower()}123")
            db.session.add(admin_user)
            print(f"Created district admin: {username} / {d.lower()}123")

    db.session.commit()
    print("All done.")