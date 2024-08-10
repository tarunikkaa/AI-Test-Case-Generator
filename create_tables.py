from app import app, db
from app import User, Feedback  # Import the models

with app.app_context():
    db.create_all()
    print("Database tables created successfully.")
