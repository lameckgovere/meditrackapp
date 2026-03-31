from app import app
from database import db
from models import District, ServicePoint, Province

def populate_service_points():
    with app.app_context():
        # Standard list of service points
        service_points_data = [
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

        # Get all districts (or only those in Midlands)
        # If you want all districts, use: districts = District.query.all()
        midlands = Province.query.filter_by(name='Midlands').first()
        if midlands:
            districts = District.query.filter_by(province_id=midlands.id).all()
        else:
            # Fallback: if Midlands not found, use all districts
            print("Midlands province not found. Using all districts.")
            districts = District.query.all()

        if not districts:
            print("No districts found. Please run populate_midlands.py first.")
            return

        for district in districts:
            for sp_data in service_points_data:
                # Check if this service point already exists in this district
                existing = ServicePoint.query.filter_by(
                    name=sp_data['name'],
                    district_id=district.id
                ).first()
                if not existing:
                    sp = ServicePoint(
                        name=sp_data['name'],
                        order=sp_data['order'],
                        default_service=sp_data['default_service'],
                        district_id=district.id
                    )
                    db.session.add(sp)
                    print(f"Added {sp_data['name']} to {district.name}")
            db.session.commit()

        print("Service points added to all districts.")

if __name__ == '__main__':
    populate_service_points()