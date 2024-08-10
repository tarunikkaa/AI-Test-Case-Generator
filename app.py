from openai import OpenAI  # Version 1.33.0
from openai.types.beta.threads.message_create_params import Attachment, AttachmentToolFileSearch
import json
import os
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from forms import LoginForm, SignupForm
from werkzeug.utils import secure_filename
import pandas as pd
from io import StringIO
from flask import session
from flask_session import Session

# Initialize the OpenAI client
MY_OPENAI_KEY = os.getenv('OPENAI_API_KEY', 'sk-...')  # Add your OpenAI API key
client = OpenAI(api_key=MY_OPENAI_KEY)

app = Flask(__name__)

# Set the secret key for CSRF
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default_secret_key')

# Configure session to use filesystem (instead of signed cookies)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# Initialize CSRF protection
csrf = CSRFProtect(app)

# SQLAlchemy configuration
file_path = os.path.abspath(os.getcwd()) + "/instance/users.db"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + file_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Define your SQLAlchemy model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feedback_text = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class UserFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    uploaded_filename = db.Column(db.String(255), nullable=False)
    generated_filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('files', lazy=True))


# Create tables if they don't exist
with app.app_context():
    db.create_all()

# Define your file upload directory
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

@app.route('/', methods=['GET', 'POST'])
def index():
    login_form = LoginForm()
    signup_form = SignupForm()
    
    if request.method == 'POST':
        if request.form.get('form_type') == 'login':
            if login_form.validate_on_submit():
                email = login_form.email.data
                password = login_form.password.data
                user = User.query.filter_by(email=email).first()
                if user and user.password == password:
                    print('Login successful', 'success')

                    session['user_id'] = user.id

                    return redirect(url_for('welcome'))
                else:
                    flash('Invalid credentials', 'error')
            else:
                flash('Login form validation failed', 'error')
        
        elif request.form.get('form_type') == 'signup':
            if signup_form.validate_on_submit():
                name = signup_form.name.data
                email = signup_form.email.data
                password = signup_form.password.data
                
                if User.query.filter_by(email=email).first():
                    flash('User already exists', 'error')
                else:
                    new_user = User(name=name, email=email, password=password)
                    db.session.add(new_user)
                    db.session.commit()
                    print('User added successfully', 'success')

                    session['user_id'] = new_user.id
                    return redirect(url_for('welcome'))
            else:
                flash('Signup form validation failed', 'error')
    
    return render_template('index.html', login_form=login_form, signup_form=signup_form)

@app.route('/welcome', methods=['GET'])
def welcome():
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            return render_template('welcome.html', user=user)
        else:
            flash('User not found', 'error')
            return redirect(url_for('index'))

@app.route('/upload', methods=['POST'])
@csrf.exempt  # Generally not recommended to disable CSRF protection
def upload():
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            if 'file' not in request.files:
                flash('No file part', 'error')
                return redirect(url_for('welcome', user_id=user.id))
            file = request.files['file']
            if file.filename == '':
                flash('No selected file', 'error')
                return redirect(url_for('welcome', user_id=user.id))

            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)

            try:
                # Upload the PDF to the OpenAI API
                openai_file = client.files.create(
                    file=open(file_path, 'rb'),
                    purpose='assistants'
                )

                # Create a new thread
                thread = client.beta.threads.create()

                # Get or create the assistant
                def get_assistant():
                    for assistant in client.beta.assistants.list():
                        if assistant.name == 'My Assistant Name':
                            return assistant
                    return client.beta.assistants.create(
                        model='gpt-4o',
                        description='You are a PDF retrieval assistant.',
                        instructions="You are a helpful assistant designed to output only CSV. Find information from the text and files provided.",
                        tools=[{"type": "file_search"}],
                        name='My Assistant Name',
                    )

                assistant = get_assistant()

                # Create a prompt for the assistant
                min_cases = int(request.form.get('quantity5', 2))
                max_cases = int(request.form.get('quantity6', 4))
                positive = request.form.get('positive', '')
                negative = request.form.get('negative', '')
                exception = request.form.get('exception', '')
                error = request.form.get('error', '')

                # Use a different variable name to avoid conflict with built-in `str`
                case_types = ", ".join(filter(None, [positive, negative, exception, error]))
                print(case_types)

                prompt = (f"Based on the document that's uploaded, generate a minimum of {min_cases} test cases, "
                        f"maximum of {max_cases} test cases, and have {case_types}. "
                        f"The attributes should be Test Case Type, Test Case Id, Test Case Name, Test Case Description, Test Steps, expected results, along with the sample data. "
                        f"The Test Case Type should only be values such as {case_types}."
                        f"Generate the output containing only the csv data and nothing else. ")

                # Create a message in the thread with the prompt and file attachment
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role='user',
                    content=prompt,
                    attachments=[Attachment(file_id=openai_file.id, tools=[AttachmentToolFileSearch(type='file_search')])]
                )

                # Run the thread with the assistant
                run = client.beta.threads.runs.create_and_poll(
                    thread_id=thread.id,
                    assistant_id=assistant.id,
                    timeout=300,  # 5 minutes
                )

                if run.status != "completed":
                    raise Exception('Run failed:', run.status)

                # Fetch outputs of the thread
                messages_cursor = client.beta.threads.messages.list(thread_id=thread.id)
                messages = [message for message in messages_cursor]

                message = messages[0]  # This is the output from the Assistant
                csv_content = message.content[0].text.value

                savefile_name = filename

                # Save the CSV content to a file
                csv_file_path = os.path.join(app.config['UPLOAD_FOLDER'], savefile_name + '-test cases-' + str(user_id) + '.csv')
                with open(csv_file_path, 'w', newline='') as csv_file:
                    csv_file.write(csv_content)

                # Read and normalize the CSV content
                lines = csv_content.split('\n')
                csv_data = [line.split(',') for line in lines]

                # Find the maximum number of columns
                max_cols = max(len(row) for row in csv_data)

                # Normalize rows to have the same number of columns
                normalized_data = [row + [''] * (max_cols - len(row)) for row in csv_data]

                # Convert to DataFrame
                df = pd.DataFrame(normalized_data[1:], columns=normalized_data[0])

                # Convert CSV to Excel using Pandas
                excel_file_path = os.path.join(app.config['UPLOAD_FOLDER'], savefile_name + '-test cases-' + str(user_id) + '.xlsx')
                df.to_excel(excel_file_path, index=False)

                if excel_file_path:
                    # Save file names in the database
                    user_file = UserFile(
                        user_id=user.id,
                        uploaded_filename=filename,
                        generated_filename=savefile_name + '-test cases-' + str(user_id) + '.xlsx'
                    )
                    db.session.add(user_file)
                    db.session.commit()

                    return render_template('welcome.html', user=user, download_link=url_for('download_file', filename=savefile_name + '-test cases-' + str(user_id) + '.xlsx'))
                else:
                    flash("Unable to send excel file to download")

                # Delete the file from OpenAI server
                client.files.delete(openai_file.id)

            except Exception as e:
                flash(f'Error processing file: {e}', 'error')

            return redirect(url_for('welcome', user_id=user.id))
        else:
            flash('User not found', 'error')
            return redirect(url_for('index'))


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

@app.route('/about', methods=['GET'])
def about():
    return render_template('about.html')

@app.route('/submit_feedback', methods=['POST'])
@csrf.exempt  # Generally not recommended to disable CSRF protection
def submit_feedback():
    feedback_text = request.form['feedback']
    if feedback_text:
        new_feedback = Feedback(feedback_text=feedback_text)
        db.session.add(new_feedback)
        db.session.commit()
        flash('Thank you for your feedback! :)', 'success')
    else:
        flash('Feedback cannot be empty', 'error')
    return redirect(url_for('contact'))

@app.route('/contact', methods=['GET'])
def contact():
    return render_template('contact.html')

@app.route('/profile', methods=['GET'])
def profile():
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            files = UserFile.query.filter_by(user_id=user.id).all()
            return render_template('profile.html', user=user, files = files)
        else:
            flash('User not found', 'error')
            return redirect(url_for('index'))
        
@app.route('/logout')
def logout():
    # Clear the session
    session.clear()
    # Flash a message (optional)
    flash('You have been logged out.', 'info')
    # Redirect to the login page or home page
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True)
