from flask import Flask, render_template, request, redirect, url_for, session, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from datetime import datetime
from ultralytics import YOLO
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
import mediapipe as mp
import time

import pymysql
import os
import cv2
from config import TWILIO_SID, TWILIO_TOKEN
call_status = ""
pymysql.install_as_MySQLdb()

app = Flask(__name__)
app.secret_key = "secretkey123"

# ---------------- DATABASE CONFIG ---------------- #

app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:haritha%400402@localhost/eldercare'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['RESULT_FOLDER'] = 'static/results'


db = SQLAlchemy(app)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULT_FOLDER'], exist_ok=True)

# ---------------- LOAD YOLO MODEL ---------------- #

model = YOLO("best.pt")
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    refine_landmarks=True
)

# ---------------- TWILIO CONFIG ---------------- #


TWILIO_PHONE = "+13185861735"

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ---------------- DATABASE MODELS ---------------- #

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    age = db.Column(db.Integer)
    gender = db.Column(db.String(20))
    language = db.Column(db.String(50))
    doctor = db.Column(db.String(100))
    phone = db.Column(db.String(20))   # NEW
    caretaker_phone = db.Column(db.String(20))

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100))
    password = db.Column(db.String(100))
    role = db.Column(db.String(20))
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'))


class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pill_name = db.Column(db.String(100))
    dosage = db.Column(db.String(100))
    description = db.Column(db.String(200))


class Schedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'))
    exact_time = db.Column(db.String(20))
    frequency = db.Column(db.String(50))
    timing = db.Column(db.String(50))
    food = db.Column(db.String(50))
    called = db.Column(db.Boolean, default=False)

class IntakeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer)
    medicine_name = db.Column(db.String(100))
    detected_name = db.Column(db.String(100))
    status = db.Column(db.String(50))
    timestamp = db.Column(db.String(50))
    schedule_time = db.Column(db.String(10))

class CallLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer)
    medicine_name = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    attempt = db.Column(db.Integer)  # 1,2,3
    called_to = db.Column(db.String(20))  # patient/caretaker
    status = db.Column(db.String(50))  # completed/no-answer
    time = db.Column(db.String(50))

# ------------ DECORATOR FOR ADMIN ONLY ------------ #

def admin_only(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            return render_template('error.html', message="Access Denied! Admin only.")
        return f(*args, **kwargs)
    return decorated_function


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function



# ---------------- TWILIO CALL FUNCTION ---------------- #
from time import sleep
def call_patient(patient, medicine):

    phone = patient.phone
    caretaker = patient.caretaker_phone
    language = patient.language
    
    last_log = CallLog.query.filter_by(
        phone=patient.phone,
        called_to="patient"
    ).order_by(CallLog.id.desc()).first()

    attempt = 1 if not last_log or last_log.attempt >= 2 else last_log.attempt + 1
    

    # ---------------- MESSAGE ---------------- #
    try:

        if language == "hindi":

            msg = f"नमस्ते। यह आपकी दवा की याद दिलाने वाली कॉल है। कृपया अपनी दवा {medicine} लें।"
            lang = "hi-IN"

        elif language == "french":

            msg = f"Bonjour. Ceci est un rappel pour votre médicament. Veuillez prendre votre médicament {medicine}."
            lang = "fr-FR"

        elif language == "spanish":

            msg = f"Hola. Este es un recordatorio de su medicina. Por favor tome su medicina {medicine}."
            lang = "es-ES"

        elif language == "german":

            msg = f"Hallo. Dies ist eine Erinnerung an Ihre Medizin. Bitte nehmen Sie Ihr Medikament {medicine}."
            lang = "de-DE"

        else:

            msg = f"Hello. This is your medicine reminder. Please take your medicine {medicine}."
            lang = "en-IN"


    # ---------------- CALL ---------------- #
        call = twilio_client.calls.create(
            twiml=f"""
<Response>
<Say language="{lang}">
{msg}
</Say>
</Response>
""",
            to="+91"+phone,
            from_=TWILIO_PHONE,
            status_callback="https://waking-fraction-cannot.ngrok-free.dev/call_status",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"]
        )

        print(f"Calling patient Attempt {attempt}")

    # ---------------- SAVE LOG ---------------- #
        log = CallLog(
           patient_id=patient.id,
           medicine_name=medicine,
           phone=phone,
           attempt=attempt,
           called_to="patient",
           status="initiated",
           time=str(datetime.now())
        )
        db.session.add(log)
        db.session.commit()

        return call.sid
    except Exception as e:
        print("Error:", e)       

# ---------------- CHECK SCHEDULE ---------------- #

from datetime import datetime, timedelta

def check_medicine_reminder():

    with app.app_context():

        now = datetime.now().strftime("%H:%M")

        schedules = Schedule.query.all()

        for s in schedules:

            schedule_time = s.exact_time
            schedule_dt = datetime.strptime(schedule_time, "%H:%M")

            reminder_time = (schedule_dt - timedelta(minutes=15)).strftime("%H:%M")

            if now == reminder_time and not s.called:

                patient = Patient.query.get(s.patient_id)
                medicine = Medicine.query.get(s.medicine_id)

                if patient and medicine and patient.phone:

                    print("Calling patient:", patient.name)


                    call_patient(patient, medicine.pill_name)

                    s.called = True
                    db.session.commit()
                    from threading import Timer
                    Timer(3600, reset_called_flag, [s.id]).start()

# ---------------- VIDEO STREAM ---------------- #

def generate_frames(patient_id):
    camera = cv2.VideoCapture(0)

    while True:
        success, frame = camera.read()
        if not success:
            break

        annotated_frame = frame.copy()
        message = ""

        results = model(frame)

        if results and results[0].probs is not None:

            probs = results[0].probs
            predicted_class = results[0].names[probs.top1]
            confidence = probs.top1conf.item()

            print(predicted_class, confidence)

            # Show prediction
            cv2.putText(annotated_frame,
                        f"{predicted_class} {confidence:.2f}",
                        (20, 80),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        (0, 255, 0),
                        2)

            # ✅ USE PASSED patient_id (NOT session)
            if confidence < 0.5:
                message = "Low confidence - show clearly"

            else:
                if patient_id:
                    try:
                        schedule = Schedule.query.filter_by(patient_id=patient_id).first()

                        if schedule:
                            medicine = Medicine.query.get(schedule.medicine_id)

                            if medicine:
                                expected_pill = medicine.pill_name.lower()

                                if expected_pill in predicted_class.lower():
                                    message = "CORRECT PILL"
                                else:
                                    message = "WRONG PILL"
                            else:
                                message = "Medicine not found"
                        else:
                            message = "No schedule found"
                    except Exception as e:
                        print("DB Error:", e)
                        message = "Database error"
                else:
                    message = "User not logged in"

        else:
            message = "No prediction"

        # Show message
        cv2.putText(annotated_frame,
                    message,
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 0, 255),
                    3)

        ret, buffer = cv2.imencode('.jpg', annotated_frame)
        frame = buffer.tobytes()

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
def detect_swallow():
    cap = cv2.VideoCapture(0)

    chin_positions = []
    start_time = time.time()

    while time.time() - start_time < 5:

        ret, frame = cap.read()
        if not ret:
            break

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = face_mesh.process(rgb)

        if results.multi_face_landmarks:
            for face_landmarks in results.multi_face_landmarks:
                h, w, _ = frame.shape

                chin = face_landmarks.landmark[152]
                chin_y = int(chin.y * h)

                chin_positions.append(chin_y)

    cap.release()

    if len(chin_positions) < 5:
        return False

    movement = max(chin_positions) - min(chin_positions)
    print("Chin movement:", movement)

    return movement > 15
# ---------------- ROUTES ---------------- #

@app.route('/')
def index():
    return render_template('index.html')


# ---------------- LOGIN ROUTES - ROLE BASED SELECTION  ---------------- #

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Redirect to home if logged in
    if session.get('user'):
        if session.get('role') == 'admin':
            return redirect(url_for('dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    return redirect(url_for('index'))


@app.route('/dashboard')
@admin_only
def dashboard():
    return render_template('dashboard.html')


@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == "POST":
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username, password=password, role='admin').first()

        if user:
            session['user'] = user.username
            session['role'] = user.role
            session['patient_id'] = user.patient_id
            return redirect(url_for('dashboard'))
        else:
            return render_template('admin_login.html', error="❌ Invalid Admin Credentials!")

    return render_template('admin_login.html')



# ---------------- REGISTER PATIENT ---------------- #

@app.route('/register', methods=['GET', 'POST'])
@admin_only
def register_patient():

    if request.method == 'POST':

        patient = Patient(
            name=request.form['name'],
            age=request.form['age'],
            gender=request.form['gender'],
            language=request.form['language'],
            doctor=request.form['doctor'],
            phone=request.form['phone'],
            caretaker_phone=request.form['caretaker_phone']
        )

        db.session.add(patient)
        db.session.commit()

        user = User(
            username=request.form['name'],
            password="1234",
            role="user",
            patient_id=patient.id
        )

        db.session.add(user)
        db.session.commit()

        return redirect(url_for('dashboard'))

    return render_template('register_patient.html')


# ---------------- ADD MEDICINE ---------------- #

@app.route('/add_medicine', methods=['GET', 'POST'])
@admin_only
def add_medicine():

    if request.method == 'POST':

        medicine = Medicine(
            pill_name=request.form['pill_name'],
            dosage=request.form['dosage'],
            description=request.form['description']
        )

        db.session.add(medicine)
        db.session.commit()

        return redirect(url_for('dashboard'))

    return render_template("add_medicine.html")


# ---------------- SCHEDULE ---------------- #

@app.route('/schedule', methods=['GET', 'POST'])
@admin_only
def schedule():

    patients = Patient.query.all()   # ✅ FETCH PATIENTS
    medicines = Medicine.query.all()

    print("Patients:", patients)  # 🔍 DEBUG

    if request.method == 'POST':

        new_schedule = Schedule(
            patient_id=request.form['patient'],   # ✅ IMPORTANT
            medicine_id=request.form['medicine'],
            exact_time=request.form['time'],
            frequency=request.form['frequency'],
            timing=request.form['timing'],
            food=request.form['food']
        )

        db.session.add(new_schedule)
        db.session.commit()

        return redirect(url_for('dashboard'))

    return render_template(
        'schedule.html',
        medicines=medicines,
        patients=patients   # ✅ PASS TO HTML
    )

# ---------------- USER LOGIN ---------------- #

@app.route('/user_login', methods=['GET', 'POST'])
def user_login():

    if request.method == "POST":

        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(
            username=username,
            password=password,
            role='user'
        ).first()

        if user:

            session['user'] = user.username
            session['role'] = user.role
            session['patient_id'] = user.patient_id

            return redirect(url_for('user_dashboard'))

    return render_template('user_login.html')



# ---------------- USER DASHBOARD ---------------- #

from datetime import datetime, timedelta

@app.route('/user_dashboard')
def user_dashboard():

    patient_id = session.get('patient_id')

    schedules = Schedule.query.filter_by(patient_id=patient_id).all()

    data = []

    now = datetime.now()

    for s in schedules:

        medicine = Medicine.query.get(s.medicine_id)
        if not medicine:
            continue

        # ⏰ Convert schedule time
        schedule_time = datetime.strptime(s.exact_time, "%H:%M")

        # 🕒 Create today's datetime for comparison
        schedule_dt = now.replace(
            hour=schedule_time.hour,
            minute=schedule_time.minute,
            second=0,
            microsecond=0
        )

        # 🔍 GET LATEST LOG
        log = IntakeLog.query.filter_by(
            patient_id=patient_id,
            medicine_name=medicine.pill_name,
            schedule_time=s.exact_time
        ).order_by(IntakeLog.id.desc()).first()

        # -------------------------------
        # 🎯 STEP 1: TIME-BASED STATUS
        # -------------------------------
        if now < schedule_dt:
            status = "Upcoming"

        elif schedule_dt <= now <= schedule_dt + timedelta(minutes=30):
            status = "Take Now"

        else:
            status = "Missed"

        # -------------------------------
        # 🎯 STEP 2: OVERRIDE WITH LOG
        # -------------------------------
        if log:

            if log.status == "correct_swallowed":
                status = "Taken"

            elif log.status == "correct_not_swallowed":
                status = "Not Swallowed"

            elif log.status == "wrong_swallowed":
                status = "Wrong Pill (Swallowed)"

            elif log.status == "wrong_not_swallowed":
                status = "Wrong Pill (Not Swallowed)"

        # -------------------------------
        # 📦 APPEND DATA
        # -------------------------------
        data.append({
            "medicine": medicine.pill_name,
            "dosage": medicine.dosage,
            "time": s.exact_time,
            "frequency": s.frequency,
            "timing": s.timing,
            "food": s.food,
            "status": status
        })

    return render_template("user_dashboard.html", data=data)

# ---------------- VIDEO ---------------- #

@app.route('/video_feed')
def video_feed():

    patient_id = session.get('patient_id')

    return Response(stream_with_context(generate_frames(patient_id)),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/take_pill')
def take_pill():
    return render_template("take_pill.html")


# ---------------- CAPTURE PILL ---------------- #

@app.route('/capture_pill')
def capture_pill():

    patient_id = session.get('patient_id')

    cap = cv2.VideoCapture(0)
    ret, frame = cap.read()
    cap.release()

    if not ret:
        return {"message": "Camera error"}

    results = model(frame)

    if results[0].probs is None:
        return {"message": "No pill detected"}

    probs = results[0].probs
    predicted_class = results[0].names[probs.top1]
    confidence = probs.top1conf.item()

    print("Predicted:", predicted_class)
    print("Confidence:", confidence)

    # 🔍 GET SCHEDULE
    schedule = Schedule.query.filter_by(patient_id=patient_id).first()
    if not schedule:
        return {"message": "No schedule found"}

    medicine = Medicine.query.get(schedule.medicine_id)
    if not medicine:
        return {"message": "Medicine not found"}

    expected_pill = medicine.pill_name.lower()

    # ⚠️ LOW CONFIDENCE CHECK
    if confidence < 0.4:
        return {"message": "⚠ Low confidence - show clearly"}

    # 🔥 DETECT SWALLOW (ALWAYS CHECK)
    swallow = detect_swallow()

    # 🎯 FINAL 4 CASE LOGIC
    if expected_pill in predicted_class.lower():

        if swallow:
            final_status = "correct_swallowed"
            message = "✅ Correct pill taken & swallowed"
        else:
            final_status = "correct_not_swallowed"
            message = "⚠ Correct pill but not swallowed"

    else:

        if swallow:
            final_status = "wrong_swallowed"
            message = "❌ Wrong pill but swallowed"
        else:
            final_status = "wrong_not_swallowed"
            message = "❌ Wrong pill and not swallowed"

    # 💾 SAVE LOG
    log = IntakeLog(
        patient_id=patient_id,
        medicine_name=medicine.pill_name,
        detected_name=predicted_class,
        status=final_status,
        schedule_time=schedule.exact_time,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    db.session.add(log)
    db.session.commit()

    return {"message": message}
@app.route('/intake_history')
def intake_history():

    patient_id = session.get('patient_id')

    logs = IntakeLog.query.filter_by(patient_id=patient_id).all()

    return render_template("intake_history.html", logs=logs)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/call_status_view')
def show_call_status():

    global call_status
    return {"status": call_status}

@app.route('/call_history')
@admin_only
def call_history():
     logs = CallLog.query.all()

     updated_logs = []

     for log in logs:
        patient = db.session.get(Patient, log.patient_id)

        updated_logs.append({
            "patient_name": patient.name if patient else "Unknown",
            "phone": log.phone,
            "attempt": log.attempt,
            "called_to": log.called_to,
            "status": log.status,
            "time": log.time
        })

     return render_template("call_history.html", logs=updated_logs)


call_results = {}  # store call results

@app.route('/call_status', methods=['POST'])
def call_status():

    call_sid = request.form.get("CallSid")
    status = request.form.get("CallStatus")
    to_number = request.form.get("To")

    print("Call Status:", status)

    phone = to_number.replace("+91", "")

    call_results[phone] = status
    #  IGNORE CARETAKER CALLBACKS
    # Check if this number belongs to caretaker
    

    # ---------------- GET LAST LOG ---------------- #
    last_log = CallLog.query.filter_by(
        phone=phone,
        called_to="patient"
    ).order_by(CallLog.id.desc()).first()

    last_any_log = CallLog.query.filter_by(
      phone=phone
    ).order_by(CallLog.id.desc()).first()
    
    if last_any_log and last_any_log.called_to == "caretaker":
      print("Ignoring caretaker callback")
      return "OK"
    if last_log:
        last_log.status = status
        db.session.commit()

    

    # ---------------- ONLY HANDLE FAILED CALLS ---------------- #
    if status in ["no-answer", "busy", "failed"] or status == "completed":

       duration = int(request.form.get("CallDuration", 0))

       last_log = CallLog.query.filter_by(
         phone=phone,
         called_to="patient"
       ).order_by(CallLog.id.desc()).first()

       attempts = last_log.attempt if last_log else 0

       print("Duration:", duration)
       print("Attempts:", attempts)

    # ❌ NOT ANSWERED
       if duration == 0:

          if attempts == 1:
            print("Retrying after 1 min...")
            from threading import Timer
            Timer(60, retry_call, [phone]).start()

          elif attempts == 2:
            print("Calling caretaker...")

            patient = Patient.query.filter_by(phone=phone).first()

            if patient and patient.caretaker_phone:
                twilio_client.calls.create(
                    twiml="""
<Response>
<Say>
Patient did not answer medicine reminder call. Please check immediately.
</Say>
</Response>
""",
                    to="+91"+patient.caretaker_phone,
                    from_=TWILIO_PHONE,
                    status_callback=" https://waking-fraction-cannot.ngrok-free.dev/call_status"
                )

                # ✅ Log caretaker call
                log = CallLog(
                    patient_id=patient.id,
                    medicine_name="Reminder",
                    phone=patient.caretaker_phone,
                    attempt=3,
                    called_to="caretaker",
                    status="called",
                    time=str(datetime.now())
                )
                db.session.add(log)
                db.session.commit()

    # ✅ ANSWERED
       else:
         print("Call attended by patient ✅")

    return "OK"

def retry_call(phone):
    import time
    #time.sleep(5)  # small delay (important)

    with app.app_context():
        print("Retry triggered for:", phone)

        patient = Patient.query.filter_by(phone=phone).first()
        if not patient:
            return

        schedule = Schedule.query.filter_by(patient_id=patient.id).first()
        if not schedule:
           return

        medicine = Medicine.query.get(schedule.medicine_id)
        if not medicine:
           return

        call_patient(patient, medicine.pill_name)


def reset_called_flag(schedule_id):
    with app.app_context():
        s = Schedule.query.get(schedule_id)
        if s:
            s.called = False
            db.session.commit()
    
# ---------------- START SCHEDULER ---------------- #

scheduler = BackgroundScheduler()
scheduler.add_job(check_medicine_reminder, 'interval', minutes=1)
scheduler.start()


# ---------------- MAIN ---------------- #

if __name__ == '__main__':

    with app.app_context():

        db.create_all()

        admin = User.query.filter_by(username="admin").first()

        if not admin:

            db.session.add(User(
                username="admin",
                password="admin",
                role="admin",
                patient_id=None
            ))

            db.session.commit()

    app.run(debug=True)